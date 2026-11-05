"""Codex 408：统一预算在真实连接上的停止边界（本机 HTTP 服务；浏览器项需要本机装有 Chromium）。

- 截止时间在读块处执行：慢速逐字节返回、发完响应头后停住不发，都在期限附近停，报 deadline，不报读到底；
- 浏览器内部跟随的重定向超出请求预算时，导航一返回就按 request_budget 停，这一页不交付。
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from biz_rules.history_models import FetchRequest
from collection import rate, site_crawler

DEADLINE = 5


class _Server:
    """/trickle 每 0.25 秒 1 字节，共 40 字节（10 秒）；/stall 发完响应头后停 20 秒；
    /midstall 第 0、2、4 秒各 1 字节后停 20 秒（Codex 418）；/hop/N 连续 302 到 /hop/0。"""

    def __init__(self):
        self.hits = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hits.append(self.path)
                try:
                    if self.path.startswith("/hop/"):
                        n = int(self.path.rsplit("/", 1)[1])
                        body = b"" if n else b"<html><body>final page</body></html>"
                        self.send_response(302 if n else 200)
                        if n:
                            self.send_header("Location", "/hop/" + str(n - 1))
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", "40")
                    self.end_headers()
                    self.wfile.flush()
                    if self.path == "/stall":
                        time.sleep(20)
                        return
                    if self.path == "/midstall":
                        for k in range(3):
                            time.sleep(2 if k else 0)
                            self.wfile.write(b"x")
                            self.wfile.flush()
                        time.sleep(20)
                        return
                    for _ in range(40):
                        time.sleep(0.25)
                        self.wfile.write(b"x")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def url(self, path):
        return "http://127.0.0.1:" + str(self.server.server_port) + path

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _read(monkeypatch, fetch, deadline=DEADLINE):
    def fetcher(config):
        fetch()
        yield {"id": "one", "title": "synthetic"}

    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_drug_label", {
        "source_type": "api", "api_fetcher": fetcher, "api": {}, "field_map": {"title": "title"}, "uid_field": "id"})
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    start = time.monotonic()
    batch = site_crawler.read_site(FetchRequest(source_id="fda_api_drug_label", query="records", max_pages=1,
                                                max_records=1, deadline_seconds=deadline))
    return batch, time.monotonic() - start


def _assert_deadline(batch, elapsed):
    assert batch["stop_reason"] == "deadline", batch
    assert not batch["upstream_pagination_exhausted"] and batch["records"] == [] and batch["error"] is None
    assert DEADLINE - 0.2 <= elapsed < DEADLINE + 1.0


@pytest.mark.parametrize("path", ["/trickle", "/stall", "/midstall"])
def test_httpx_stream_stops_at_the_run_deadline(monkeypatch, path):
    with _Server() as server, httpx.Client(trust_env=False, timeout=60.0) as client:
        batch, elapsed = _read(monkeypatch, lambda: client.get(server.url(path)))
    _assert_deadline(batch, elapsed)
    assert len(server.hits) == 1  # 到期不重试


@pytest.mark.parametrize("path", ["/trickle", "/stall", "/midstall"])
def test_requests_stream_stops_at_the_run_deadline(monkeypatch, path):
    requests = pytest.importorskip("requests")
    with _Server() as server, requests.Session() as session:
        session.trust_env = False
        batch, elapsed = _read(monkeypatch, lambda: session.get(server.url(path), timeout=60))
    _assert_deadline(batch, elapsed)


@pytest.mark.parametrize("path", ["/trickle", "/stall", "/midstall"])
def test_curl_transfer_stops_at_the_run_deadline(monkeypatch, path):
    curl = pytest.importorskip("curl_cffi.requests")
    with _Server() as server, curl.Session() as session:
        batch, elapsed = _read(monkeypatch, lambda: session.get(server.url(path), timeout=60))
    _assert_deadline(batch, elapsed)


@pytest.mark.parametrize("library", ["playwright", "patchright"])
def test_browser_redirects_past_the_request_budget_stop_the_read(monkeypatch, library):
    sync_api = pytest.importorskip(library + ".sync_api")

    def browse():
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, args=["--no-proxy-server"])
            except Exception as exc:  # noqa: BLE001
                pytest.skip("Chromium 不可用：" + type(exc).__name__)
            try:
                browser.new_page().goto(server.url("/hop/12"), wait_until="load", timeout=15000)
            finally:
                browser.close()

    with _Server() as server:
        batch, _ = _read(monkeypatch, browse, deadline=30)
    assert len(server.hits) == 13  # 1 次过闸 + 12 跳浏览器内部跟随（Chromium 发出前拦不到）
    assert batch["requests"] == 13
    assert batch["stop_reason"] == "request_budget"
    assert not batch["upstream_pagination_exhausted"] and batch["records"] == [] and batch["error"] is None
    assert batch["next_continuation"] is not None
