"""MinerU 云端精准解析 API 客户端（申请上传链接 → PUT → 轮询 → 下载 zip 取 full.md）。不联网。"""

import io
import json
import zipfile

import httpx

from processing import mineru_cloud


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
    assert out["success"] is False and out["error"] == "cloud_failed"
    mock, _ = transport([], apply_code=-1)
    with httpx.Client(transport=mock) as client:
        out = mineru_cloud.parse(write_doc(tmp_path), token="bad", client=client, poll_seconds=0, timeout_seconds=30)
    assert out["success"] is False and out["error"] == "cloud_rejected"


def test_timeout_is_reported(tmp_path):
    mock, _ = transport(["running"] * 50)
    with httpx.Client(transport=mock) as client:
        out = mineru_cloud.parse(write_doc(tmp_path), token="T", client=client, poll_seconds=0, timeout_seconds=0)
    assert out["success"] is False and out["error"] == "cloud_timeout"
