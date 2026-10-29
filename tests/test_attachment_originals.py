"""实际下载函数＋HTTP替身；文件只写入pytest临时目录。"""

from pathlib import Path

import httpx
import pytest

from core_engine import scrapling_client as client
from flows import crawl_flow

URL = "https://example.invalid/notice.pdf"
A = b"%PDF-1.4\nSYNTHETIC evidence A\n%%EOF\n"
B = b"%PDF-1.4\nSYNTHETIC evidence B\n%%EOF\n"


@pytest.fixture
def http_source(monkeypatch, tmp_path):
    monkeypatch.setattr(client.APP, "DATA_ROOT", tmp_path)
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
    return client.download_pdf.fn(URL, source_site="synthetic_files", **kwargs)


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


@pytest.mark.parametrize("force", [False, True])
def test_existing_legacy_file_and_new_original_are_never_overwritten(http_source, force):
    legacy = client.APP.RAW_DIR / "synthetic_files" / (client.make_uuid(URL) + ".pdf")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(A)
    http_source["body"] = B
    result = download(force=force)
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


def test_timeout_uses_existing_bounded_retry_and_never_calls_parser(http_source, monkeypatch):
    from unittest.mock import Mock

    first = download()
    http_source["error"] = httpx.ReadTimeout("synthetic read timeout")
    monkeypatch.setattr(client.APP, "TASK_RETRIES", 2)
    monkeypatch.setattr(client.APP, "TASK_RETRY_DELAY", .01)
    delays = []
    monkeypatch.setattr(crawl_flow.time, "sleep", delays.append)
    parser = Mock(side_effect=AssertionError("Must not parse an unconfirmed cached file"))
    monkeypatch.setattr(crawl_flow.route_pdf, "fn", parser)
    with pytest.raises(httpx.ReadTimeout):
        crawl_flow.fetch_and_parse_attachments.fn(
            [{"url": URL}], "parent", "synthetic_files",
        )
    assert len(http_source["requests"]) == 4  # 初次成功＋本轮最多三次读取
    assert delays == [.01, .02]
    parser.assert_not_called()
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
    monkeypatch.setattr(crawl_flow.time, "sleep", delays.append)
    result = crawl_flow._read_with_retries(
        client.download_pdf.fn, pdf_url=URL, source_site="synthetic_files",
    )
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


def test_force_refetch_of_same_bytes_does_not_rewrite_original(http_source):
    first = download()
    path = Path(first["local_path"])
    before = path.stat()
    repeated = download(force=True)
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
