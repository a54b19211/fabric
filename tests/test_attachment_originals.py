"""实际下载函数＋HTTP替身；文件只写入pytest临时目录。"""

from pathlib import Path

import httpx
import pytest

from fetch import transport
from fetch import web_client as client

URL = "https://example.invalid/notice.pdf"
A = b"%PDF-1.4\nSYNTHETIC evidence A\n%%EOF\n"
B = b"%PDF-1.4\nSYNTHETIC evidence B\n%%EOF\n"


@pytest.fixture
def http_source(monkeypatch, tmp_path):
    from fetch import rate

    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setattr(client.APP, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(client.APP, "TASK_RETRY_DELAY", 0)  # transport.get 重发 429 之前不真等
    state = {"body": A, "status": 200, "error": None, "requests": []}
    real_client = httpx.Client
    state["client_class"] = real_client

    def handle(request):
        state["requests"].append(request)
        if state["error"]:
            raise state["error"]
        return httpx.Response(state["status"], content=state["body"],
                              headers={"content-type": "application/pdf"})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(client.httpx, "Client",
                        lambda **kwargs: real_client(transport=transport, **kwargs))
    return state


def download(**kwargs):
    return client.download_pdf(URL, source_site="synthetic_files", **kwargs)


def test_a_connection_that_does_not_come_up_is_given_up_after_ten_seconds_so_the_transport_can_try_another(
        http_source):
    """nccn.org 的个别连接 TLS 握手卡 60 秒以上（实测 62-76 秒）：下载的连接超时只有 10 秒，超时了 transport.get 换一条重发。"""
    download()
    timeout = http_source["requests"][0].extensions["timeout"]
    assert timeout["connect"] == client.CONNECT_SECONDS == 10 and timeout["read"] == 120


def test_same_url_new_bytes_are_used_and_old_original_is_retained(http_source):
    first = download()
    assert Path(first["local_path"]).read_bytes() == A
    http_source["body"] = B
    second = download()
    observed = Path(second["local_path"]).read_bytes()
    print({"first_path": first["local_path"], "second_path": second["local_path"],
           "server_returned": "B", "download_returned": "B" if observed == B else "A",
           "requests": len(http_source["requests"])})
    assert observed == B
    assert first["uuid"] == second["uuid"]
    assert first["local_path"] != second["local_path"]
    assert Path(first["local_path"]).read_bytes() == A
    third = download()
    assert third["local_path"] == second["local_path"]
    assert third["skipped"] is True
    assert len(http_source["requests"]) == 3


def test_existing_legacy_file_and_new_original_are_never_overwritten(http_source):
    legacy = client.APP.RAW_DIR / "synthetic_files" / (client.make_uuid(URL) + ".pdf")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(A)
    http_source["body"] = B
    result = download()
    assert legacy.read_bytes() == A
    assert Path(result["local_path"]).read_bytes() == B
    assert Path(result["local_path"]) != legacy


@pytest.mark.parametrize("status", [304, 404, 429, 500, 503])
def test_http_failure_never_returns_cached_a(http_source, status):
    first = download()
    directory = Path(first["local_path"]).parent
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    http_source["status"] = status
    with pytest.raises(httpx.HTTPStatusError):
        download()
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_timeout_uses_the_bounded_retry_and_keeps_the_old_original(http_source, monkeypatch):
    first = download()
    http_source["error"] = httpx.ReadTimeout("synthetic read timeout")
    monkeypatch.setattr(client.APP, "TASK_RETRIES", 2)
    monkeypatch.setattr(client.APP, "TASK_RETRY_DELAY", .01)
    delays = []
    monkeypatch.setattr(transport.time, "sleep", delays.append)
    with pytest.raises(httpx.ReadTimeout):
        download()
    assert len(http_source["requests"]) == 4  # 初次成功＋本轮最多三次读取
    assert delays == []  # 超时马上换一条连接重发，不干等；只有 429 才等
    assert Path(first["local_path"]).read_bytes() == A


def test_429_retries_once_then_reads_b(http_source, monkeypatch):

    first = download()
    # Reuse the actual download function, replacing only the HTTP transport factory.
    httpx_client = http_source["client_class"]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429 if len(requests) == 1 else 200, content=B,
                              headers={"content-type": "application/pdf"})

    monkeypatch.setattr(client.httpx, "Client",
                        lambda **kwargs: httpx_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(client.APP, "TASK_RETRIES", 2)
    monkeypatch.setattr(client.APP, "TASK_RETRY_DELAY", .01)
    delays = []
    monkeypatch.setattr(transport.time, "sleep", delays.append)
    result = download()
    assert len(requests) == 2 and delays == [.01]
    assert Path(result["local_path"]).read_bytes() == B
    assert Path(first["local_path"]).read_bytes() == A


def test_interrupted_local_publish_leaves_a_and_cleans_temporary_file(http_source, monkeypatch):
    first = download()
    directory = Path(first["local_path"]).parent
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    http_source["body"] = B

    def fail_link(*_args):
        raise OSError("synthetic interrupted publication")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(client.os, "link", fail_link)
        with pytest.raises(OSError, match="interrupted"):
            download()
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    recovered = download()
    assert Path(recovered["local_path"]).read_bytes() == B
    assert Path(first["local_path"]).read_bytes() == A
    assert not list(directory.glob(".download-*"))


def test_existing_damaged_original_is_not_silently_reused_or_overwritten(http_source):
    first = download()
    path = Path(first["local_path"])
    path.write_bytes(b"synthetic local corruption")
    with pytest.raises(OSError, match="摘要不符"):
        download()
    assert path.read_bytes() == b"synthetic local corruption"


def test_empty_response_is_a_failure_with_old_a_intact(http_source):
    first = download()
    http_source["body"] = b""
    with pytest.raises(ValueError, match="响应为空"):
        download()
    assert Path(first["local_path"]).read_bytes() == A


def test_refetch_of_same_bytes_does_not_rewrite_original(http_source):
    first = download()
    path = Path(first["local_path"])
    before = path.stat()
    repeated = download()
    after = path.stat()
    assert repeated["local_path"] == first["local_path"]
    assert repeated["skipped"] is True
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert len(http_source["requests"]) == 2
    assert all(request.headers["cache-control"] == "no-cache" for request in http_source["requests"])


def test_concurrent_same_content_publishes_one_complete_original(http_source, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    original_link = client.os.link

    def synchronized_link(source, target):
        barrier.wait(timeout=5)
        original_link(source, target)

    monkeypatch.setattr(client.os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: download(), [1, 2]))
    assert results[0]["local_path"] == results[1]["local_path"]
    assert sorted(result["skipped"] for result in results) == [False, True]
    directory = Path(results[0]["local_path"]).parent
    assert len(list(directory.iterdir())) == 1
    assert Path(results[0]["local_path"]).read_bytes() == A


def test_pdf_header_keeps_content_path_stable_despite_http_type_change(http_source, monkeypatch):
    first = download()
    httpx_client = http_source["client_class"]
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=A, headers={"content-type": "application/msword",
                                "content-disposition": 'attachment; filename="notice.doc"'}))
    monkeypatch.setattr(client.httpx, "Client",
                        lambda **kwargs: httpx_client(transport=transport, **kwargs))
    repeated = download()
    assert repeated["local_path"] == first["local_path"] and repeated["file_ext"] == ".pdf"


def test_a_link_that_answers_with_a_web_page_is_not_stored_as_an_attachment(http_source, tmp_path):
    """CDE: the related-news links of a page were stored as 33 attachments that are HTML pages. A link whose answer
    is HTML and whose address is not an .htm file is another page; an .htm file (HKEX) is still an attachment."""
    http_source["body"] = b"<!DOCTYPE html>\r\n<html lang=\"zh\"><head></head><body>news</body></html>"
    with pytest.raises(ValueError, match="html_page_not_a_file"):
        client.download_pdf("https://www.cde.org.cn/main/news/viewInfoCommon/12842c9b", source_site="synthetic_files")
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]  # nothing written
    stored = client.download_pdf("https://www1.hkexnews.hk/listedco/2026/1105/a_c.htm", source_site="synthetic_files")
    assert Path(stored["local_path"]).is_file()


def test_an_attachment_that_is_a_web_page_gets_the_html_extension_and_not_bin():
    """HKEX announcements are sometimes .htm pages: the content type says what they are."""
    from types import SimpleNamespace

    page = SimpleNamespace(headers={"content-type": "text/html; charset=UTF-8"})
    assert client._ext_from_response(page, "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/1105/a_c.htm") == ".html"
    assert client._ext_from_response(SimpleNamespace(headers={"content-type": "application/pdf"}), "https://x.invalid/y") == ".pdf"
    assert client._ext_from_response(SimpleNamespace(headers={}), "https://x.invalid/y") == ".bin"  # nothing says what it is
