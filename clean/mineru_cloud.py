"""MinerU 云端精准解析 API（https://mineru.net/apiManage/docs）的最小客户端。

流程：POST /api/v4/file-urls/batch 申请上传链接 → PUT 文件 → 轮询 /api/v4/extract-results/batch/{batch_id}
→ 下载 full_zip_url，取 full.md。Token 是 API 管理页面创建的 Bearer token，只从环境变量
DATAFABRIC_MINERU_CLOUD_TOKEN 读，不写进代码或日志。文件会离开内网：只用于公开来源的原件。

官方限制：单文件 ≤200MB、≤200 页；每账号每天 1000 页高优先级额度；提交类接口（单个建任务、批量上传、批量 URL）
合计每分钟 50 个文件，每用户每天最多 5000 个文件（其中 html ≤100）；查结果接口合计每分钟 1000 次。
本客户端在本机所有进程之间共用一个限流状态文件（DATAFABRIC_MINERU_STATE，默认
~/.local/state/datafabric/mineru_cloud.json，flock 加锁）：每分钟最多 50 次提交、每天（北京时间）最多 5000 个、
html 最多 100 个；轮询间隔至少 5 秒。

返回 {"success", "markdown", "error"}；error 带上真实原因（HTTP 状态、MinerU code/msg、state/err_msg，截断，
不含 token），例如 "cloud_rejected:-60005:文件大小超出限制"。额度/限频/服务暂不可用时另带 "deferred": True
和 "retry_at"（epoch 秒）：这不是文件的问题，调用方不应把它记作一次失败；同时状态文件记下暂停时间，
所有进程在此之前都不再提交（当日额度用完 → 次日北京时间 0 点）。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import io
import json
import os
import time
import zipfile
from pathlib import Path

import httpx

BASE = "https://mineru.net"
MAX_BYTES = 200 * 1024 * 1024
PER_MINUTE = 50
PER_DAY = 5000
HTML_PER_DAY = 100
MIN_POLL_SECONDS = 5
PAUSE_SECONDS = 600  # 限频、队列满、服务暂不可用：所有进程暂停 10 分钟
DAILY_CODES = {"-60018", "-60019"}  # 每日任务数 / html 额度用完：明日再来
TRANSIENT_CODES = {"-10001", "-60001", "-60007", "-60009", "-60020", "-60021", "-60022"}
TOKEN_CODES = {"A0202", "A0211"}  # token 错误/过期：配置问题，不算文件失败
BEIJING = dt.timezone(dt.timedelta(hours=8))


def _fail(code, detail=""):
    return {"success": False, "markdown": "", "error": _reason(code, detail)}


def _reason(code, detail=""):
    detail = " ".join(str(detail).split())
    return (code + (":" + detail if detail else ""))[:160]


def _defer(code, detail, retry_at):
    out = _fail(code, detail)
    out.update(deferred=True, retry_at=retry_at)
    return out


def _next_day():
    now = dt.datetime.now(BEIJING)
    return (now.replace(hour=0, minute=5, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()


def _state_path():
    return Path(os.environ.get("DATAFABRIC_MINERU_STATE")
                or Path.home() / ".local/state/datafabric/mineru_cloud.json")


@contextlib.contextmanager
def _state():
    """跨进程共用的限流状态（flock 锁住读改写）。"""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            state = {}
        yield state
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)


def _reserve(is_html, *, now=time.time, sleep=time.sleep):
    """占一个提交名额：每分钟满了就等；当天满了或处在暂停期返回 deferred 结果，否则返回 None。"""
    while True:
        with _state() as state:
            t = now()
            day = dt.datetime.fromtimestamp(t, BEIJING).date().isoformat()
            if state.get("day") != day:
                state.update(day=day, count=0, html=0)
            if state.get("paused_until", 0) > t:
                return _defer("cloud_deferred", state.get("pause_reason", ""), state["paused_until"])
            if state["count"] >= PER_DAY or (is_html and state["html"] >= HTML_PER_DAY):
                if not is_html:
                    state.update(paused_until=_next_day(), pause_reason="local_daily_cap")
                return _defer("cloud_deferred", "local_daily_cap", _next_day())
            recent = [s for s in state.get("recent", []) if s > t - 60]
            if len(recent) < PER_MINUTE:
                state.update(recent=recent + [t], count=state["count"] + 1, html=state["html"] + int(is_html))
                return None
            state["recent"] = recent
            wait = recent[0] + 60 - t
        sleep(max(wait, 0.5))


def _pause(until, reason):
    with _state() as state:
        state.update(paused_until=max(until, state.get("paused_until", 0)), pause_reason=reason)


def _classify(code, msg, status=None):
    """申请上传链接失败的结果：额度/限频/暂不可用 → deferred（并让所有进程暂停），其他 → 失败。"""
    detail = ":".join(str(x) for x in (status, code, msg) if x not in (None, ""))
    code = str(code) if code is not None else ""
    if code in DAILY_CODES:
        until = _next_day()
    elif code in TRANSIENT_CODES or code in TOKEN_CODES or status in (429,) or (status or 0) >= 500:
        until = time.time() + PAUSE_SECONDS
    else:
        return _fail("cloud_rejected", detail)
    _pause(until, detail[:80])
    return _defer("cloud_rejected", detail, until)


def parse(path, *, token=None, client=None, model_version="pipeline", poll_seconds=MIN_POLL_SECONDS,
          timeout_seconds=600):
    token = token or os.environ.get("DATAFABRIC_MINERU_CLOUD_TOKEN", "")
    if not token:
        return _fail("cloud_not_configured")
    file = Path(path)
    if file.stat().st_size > MAX_BYTES:
        return _fail("cloud_file_too_large")
    busy = _reserve(file.suffix.lower() in (".html", ".htm"))
    if busy:
        return busy
    own = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        try:
            applied = client.post(
                BASE + "/api/v4/file-urls/batch",
                headers=headers,
                json={"files": [{"name": file.name, "data_id": file.stem[:64]}], "model_version": model_version},
            )
        except httpx.HTTPError as exc:
            return _classify(None, type(exc).__name__, 599)
        try:
            body = applied.json()
        except ValueError:
            body = {}
        if applied.status_code != 200 or body.get("code") != 0:
            return _classify(body.get("code"), body.get("msg") or applied.text[:60], applied.status_code)
        batch_id = body["data"]["batch_id"]
        upload = client.put(body["data"]["file_urls"][0], content=file.read_bytes())
        if upload.status_code != 200:
            return _fail("cloud_upload_failed", upload.status_code)
        return _wait_for_result(client, headers, batch_id, poll_seconds, timeout_seconds)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, zipfile.BadZipFile) as exc:
        return _fail("cloud_error", type(exc).__name__ + ":" + str(exc)[:60])
    finally:
        if own:
            client.close()


def _poll(client, headers, batch_id):
    """一次轮询：返回第一个文件的结果；临时错误（网络、5xx、非 JSON、code≠0）返回 None 并带上原因。"""
    try:
        polled = client.get(BASE + "/api/v4/extract-results/batch/" + batch_id, headers=headers)
        body = polled.json()
    except (httpx.HTTPError, ValueError) as exc:
        return None, type(exc).__name__
    if polled.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
        return None, "{}:{}".format(polled.status_code, body.get("code") if isinstance(body, dict) else "")
    results = (body.get("data") or {}).get("extract_result") or []
    return (results[0] if results else None), "no_result"


def _wait_for_result(client, headers, batch_id, poll_seconds, timeout_seconds):
    """Poll until done (then read its archive), failed, or past the deadline; transient poll errors are retried."""
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while True:
        result, why = _poll(client, headers, batch_id)
        state = (result or {}).get("state")
        if state == "done":
            archive = client.get(result["full_zip_url"])
            return _read_archive(archive.content)
        if state == "failed":
            err = result.get("err_msg") or ""
            code = str(result.get("err_code") or "")
            if code in DAILY_CODES or any(c in err for c in DAILY_CODES):
                _pause(_next_day(), "daily:" + err[:60])
                return _defer("cloud_failed", (code + ":" + err).strip(":"), _next_day())
            return _fail("cloud_failed", (code + ":" + err).strip(":"))
        last = state or why
        if time.monotonic() >= deadline:
            return _fail("cloud_timeout", last)
        time.sleep(poll_seconds)


def _read_archive(content):
    """full.md of the result archive, with the content list (each block's page_idx) when there is one."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = [n for n in z.namelist() if n.endswith("full.md")]
        if not names:
            return _fail("cloud_no_markdown")
        out = {"success": True, "markdown": z.read(names[0]).decode("utf-8"), "error": ""}
        lists = [n for n in z.namelist() if n.endswith("content_list.json")]
        if lists:  # 每个内容块的 page_idx，加工层用来定位
            try:
                out["content_list"] = json.loads(z.read(sorted(lists, key=len)[0]))
            except ValueError:
                out["content_list"] = None
        return out
