"""MinerU 云端精准解析 API（https://mineru.net/apiManage/docs）的最小客户端。

流程：POST /api/v4/file-urls/batch 申请上传链接 → PUT 文件 → 轮询 /api/v4/extract-results/batch/{batch_id}
→ 下载 full_zip_url，取 full.md。Token 是 API 管理页面创建的 Bearer token，只从环境变量
DATAFABRIC_MINERU_CLOUD_TOKEN 读，不写进代码或日志。文件会离开内网：只用于公开来源的原件。
单文件 ≤200MB、≤200 页；每账号每天 1000 页高优先级额度。
"""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path

import httpx

BASE = "https://mineru.net"
MAX_BYTES = 200 * 1024 * 1024


def _fail(code):
    return {"success": False, "markdown": "", "error": code}


def parse(path, *, token=None, client=None, model_version="pipeline", poll_seconds=5, timeout_seconds=600):
    token = token or os.environ.get("DATAFABRIC_MINERU_CLOUD_TOKEN", "")
    if not token:
        return _fail("cloud_not_configured")
    file = Path(path)
    if file.stat().st_size > MAX_BYTES:
        return _fail("cloud_file_too_large")
    own = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        applied = client.post(
            BASE + "/api/v4/file-urls/batch",
            headers=headers,
            json={"files": [{"name": file.name, "data_id": file.stem[:64]}], "model_version": model_version},
        )
        body = applied.json() if applied.status_code == 200 else {}
        if body.get("code") != 0:
            return _fail("cloud_rejected")
        batch_id = body["data"]["batch_id"]
        upload = client.put(body["data"]["file_urls"][0], content=file.read_bytes())
        if upload.status_code != 200:
            return _fail("cloud_upload_failed")
        return _wait_for_result(client, headers, batch_id, poll_seconds, timeout_seconds)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, zipfile.BadZipFile):
        return _fail("cloud_error")
    finally:
        if own:
            client.close()


def _wait_for_result(client, headers, batch_id, poll_seconds, timeout_seconds):
    """Poll the batch until it is done (then read its archive), failed, or past the deadline."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        polled = client.get(BASE + "/api/v4/extract-results/batch/" + batch_id, headers=headers).json()
        results = (polled.get("data") or {}).get("extract_result") or []
        state = results[0].get("state") if results else None
        if state == "done":
            archive = client.get(results[0]["full_zip_url"])
            return _read_archive(archive.content)
        if state == "failed":
            return _fail("cloud_failed")
        if time.monotonic() >= deadline:
            return _fail("cloud_timeout")
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
