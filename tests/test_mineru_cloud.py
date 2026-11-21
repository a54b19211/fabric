"""MinerU 云端精准解析 API 客户端（申请上传链接 → PUT → 轮询 → 下载 zip 取 full.md）。不联网。"""

import io
import json
import time
import zipfile

import httpx

from clean import mineru_cloud


def zipped(md):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("full.md", md)
        z.writestr("layout.json", "{}")
    return buf.getvalue()


def transport(states, *, apply_code=0):
    seen = []

    def handle(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/api/v4/file-urls/batch":
            body = json.loads(request.content)
            assert body["files"][0]["name"].endswith(".doc") and body["model_version"] == "pipeline"
            return httpx.Response(200, json={"code": apply_code, "msg": "ok" if not apply_code else "A0202",
                                              "data": {"batch_id": "b1", "file_urls": ["https://upload.invalid/put"]}})
        if request.url.host == "upload.invalid":
            assert request.content.startswith(b"\xd0\xcf")
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/b1":
            state = states.pop(0)
            result = {"file_name": "x.doc", "state": state, "err_msg": "bad file" if state == "failed" else ""}
            if state == "done":
                result["full_zip_url"] = "https://cdn.invalid/r.zip"
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "b1", "extract_result": [result]}})
        if request.url.host == "cdn.invalid":
            return httpx.Response(200, content=zipped("# 云端结果\n\n正文"))
        return httpx.Response(404)

    return httpx.MockTransport(handle), seen


def write_doc(tmp_path):
    path = tmp_path / "x.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0 synthetic")
    return str(path)


def test_upload_poll_and_read_full_md(tmp_path):
    mock, seen = transport(["waiting-file", "running", "done"])
    with httpx.Client(transport=mock) as client:
        out = mineru_cloud.parse(write_doc(tmp_path), token="T", client=client, poll_seconds=0, timeout_seconds=30)
    assert out == {"success": True, "markdown": "# 云端结果\n\n正文", "error": ""}
    assert seen[0][2] == "Bearer T"
    assert [s[1] for s in seen].count("/api/v4/extract-results/batch/b1") == 3


def test_failed_state_and_rejected_token_are_errors_not_exceptions(tmp_path):
    mock, _ = transport(["failed"])
    with httpx.Client(transport=mock) as client:
        out = mineru_cloud.parse(write_doc(tmp_path), token="T", client=client, poll_seconds=0, timeout_seconds=30)
    assert out["success"] is False and out["error"] == "cloud_failed:bad file"
    mock, _ = transport([], apply_code=-1)
    with httpx.Client(transport=mock) as client:
        out = mineru_cloud.parse(write_doc(tmp_path), token="bad", client=client, poll_seconds=0, timeout_seconds=30)
    assert out["success"] is False and out["error"] == "cloud_rejected:200:-1:A0202"


def test_timeout_is_reported(tmp_path):
    mock, _ = transport(["running"] * 50)
    with httpx.Client(transport=mock) as client:
        out = mineru_cloud.parse(write_doc(tmp_path), token="T", client=client, poll_seconds=0, timeout_seconds=0)
    assert out["success"] is False and out["error"] == "cloud_timeout:running"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_MINERU_STATE", str(tmp_path / "state" / "mineru.json"))


def respond(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rejection_keeps_code_and_msg(tmp_path):
    client = respond(lambda r: httpx.Response(200, json={"code": -60005, "msg": "文件大小超出限制"}))
    out = mineru_cloud.parse(write_doc(tmp_path), token="SECRET", client=client)
    assert out["error"] == "cloud_rejected:200:-60005:文件大小超出限制" and "deferred" not in out
    assert "SECRET" not in json.dumps(out)


def test_daily_quota_defers_and_pauses_all_submissions(tmp_path):
    calls = []
    client = respond(lambda r: calls.append(r) or httpx.Response(200, json={"code": -60018, "msg": "每日解析任务数量已达上限"}))
    out = mineru_cloud.parse(write_doc(tmp_path), token="T", client=client)
    assert out["deferred"] is True and out["retry_at"] > time.time() + 3600 * 0
    again = mineru_cloud.parse(write_doc(tmp_path), token="T", client=client)
    assert again["deferred"] is True and len(calls) == 1  # 暂停期内不再提交


def test_429_is_deferred_not_failed(tmp_path):
    out = mineru_cloud.parse(write_doc(tmp_path), token="T", client=respond(lambda r: httpx.Response(429, text="slow")))
    assert out["deferred"] is True and out["error"].startswith("cloud_rejected:429")


def test_transient_poll_errors_are_retried(tmp_path):
    mock, seen = transport(["running", "done"])
    flaky = iter([httpx.Response(200, text="<html>busy</html>"), httpx.Response(502), None, None])

    def handle(request):
        if request.url.path.startswith("/api/v4/extract-results"):
            nxt = next(flaky)
            if nxt is not None:
                return nxt
        return mock.handle_request(request)

    out = mineru_cloud.parse(write_doc(tmp_path), token="T", client=respond(handle), poll_seconds=0, timeout_seconds=30)
    assert out["success"] is True


def test_failed_state_records_err_msg(tmp_path):
    mock, _ = transport(["failed"])
    with httpx.Client(transport=mock) as client:
        out = mineru_cloud.parse(write_doc(tmp_path), token="T", client=client, poll_seconds=0, timeout_seconds=30)
    assert out["error"] == "cloud_failed:bad file"


def test_rate_limit_waits_for_minute_slot_and_daily_cap_defers():
    clock = [1_800_000_000.0]
    waits = []

    def sleep(s):
        waits.append(s)
        clock[0] += s

    for _ in range(mineru_cloud.PER_MINUTE):
        assert mineru_cloud._reserve(False, now=lambda: clock[0], sleep=sleep) is None
    assert mineru_cloud._reserve(False, now=lambda: clock[0], sleep=sleep) is None and waits and waits[0] >= 59
    with mineru_cloud._state() as state:
        state["count"] = mineru_cloud.PER_DAY
    assert mineru_cloud._reserve(False, now=lambda: clock[0], sleep=sleep)["deferred"] is True


def test_html_cap_only_blocks_html():
    with mineru_cloud._state() as state:
        state.update(day=__import__("datetime").datetime.now(mineru_cloud.BEIJING).date().isoformat(),
                     count=0, html=mineru_cloud.HTML_PER_DAY)
    assert mineru_cloud._reserve(True)["deferred"] is True
    assert mineru_cloud._reserve(False) is None


def test_pdf_over_200_pages_is_not_submitted(tmp_path, monkeypatch):
    path = tmp_path / "big.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    monkeypatch.setattr(mineru_cloud, "_pages", lambda f: 350)
    out = mineru_cloud.parse(str(path), token="T", client=respond(lambda r: 1 / 0))
    assert out["error"] == "cloud_too_many_pages:350"
