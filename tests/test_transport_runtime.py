"""Codex 408：统一预算在真实连接上的停止边界（本机 HTTP 服务；浏览器项需要本机装有 Chromium）。

- 截止时间在读块处执行：慢速逐字节返回、发完响应头后停住不发，都在期限附近停，报 deadline，不报读到底；
- 浏览器内部跟随的重定向（发出前拦不到）照实计入请求数；请求次数不设额度，不据此停。
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from biz_rules.history_models import FetchRequest
from collection import rate, site_crawler

DEADLINE = 5
SLOW = ["/trickle", "/stall", "/midstall", "/slowhead", "/chunked"]


class _Server:
    """截止时间矩阵（Codex 418/421），期限 5 秒，每条都比期限长：
    /trickle 正文每 0.25 秒 1 字节，共 40 字节（10 秒）；/stall 发完响应头后停 20 秒；
    /midstall 正文第 0、2、4 秒各 1 字节后停 20 秒；/slowhead 响应头的一行每 0.25 秒 1 字节，共 32 字节（8 秒）；
    /chunked 声明一个 32 字节的块，块内每 0.25 秒 1 字节（8 秒）。/hop/N 连续 302 到 /hop/0。"""

    def __init__(self):
        self.hits = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def drip(self, n):
                for _ in range(n):
                    time.sleep(0.25)
                    self.wfile.write(b"x")
                    self.wfile.flush()

            def do_GET(self):
                hits.append(self.path)
                try:
                    if self.path == "/slowhead":
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                        self.drip(32)
                        self.wfile.write(b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                        return
                    if self.path == "/chunked":
                        self.send_response(200)
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        self.wfile.write(b"20\r\n")
                        self.drip(32)
                        self.wfile.write(b"\r\n0\r\n\r\n")
                        return
                    if self.path == "/page":  # 自己站的一个 fetch，加一个别的主机（localhost）的"广告"fetch
                        body = ("<html><body><script>fetch('/own');fetch('http://localhost:%d/ad');</script>ok"
                                "</body></html>" % self.server.server_port).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if self.path in ("/own", "/ad"):
                        self.send_response(200)
                        self.send_header("Content-Length", "2")
                        self.end_headers()
                        self.wfile.write(b"ok")
                        return
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
                    self.drip(40)
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


def _read(monkeypatch, fetch, deadline=DEADLINE, url_pattern=""):
    def fetcher(config):
        fetch()
        yield {"id": "one", "title": "synthetic"}

    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_drug_label", {
        "source_type": "api", "api_fetcher": fetcher, "api": {}, "field_map": {"title": "title"}, "uid_field": "id",
        "url_pattern": url_pattern})
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    start = time.monotonic()
    batch = site_crawler.read_site(FetchRequest(source_id="fda_api_drug_label", query="records", max_pages=1,
                                                max_records=1, deadline_seconds=deadline))
    return batch, time.monotonic() - start


def _assert_deadline(batch, elapsed):
    assert batch["stop_reason"] == "deadline", batch
    assert not batch["upstream_pagination_exhausted"] and batch["records"] == [] and batch["error"] is None
    assert DEADLINE - 0.2 <= elapsed < DEADLINE + 1.0


@pytest.mark.parametrize("path", SLOW)
def test_httpx_stream_stops_at_the_run_deadline(monkeypatch, path):
    with _Server() as server, httpx.Client(trust_env=False, timeout=60.0) as client:
        batch, elapsed = _read(monkeypatch, lambda: client.get(server.url(path)))
    _assert_deadline(batch, elapsed)
    assert len(server.hits) == 1  # 到期不重试


@pytest.mark.parametrize("path", SLOW)
def test_requests_stream_stops_at_the_run_deadline(monkeypatch, path):
    requests = pytest.importorskip("requests")
    with _Server() as server, requests.Session() as session:
        session.trust_env = False
        batch, elapsed = _read(monkeypatch, lambda: session.get(server.url(path), timeout=60))
    _assert_deadline(batch, elapsed)


@pytest.mark.parametrize("path", SLOW)
def test_curl_transfer_stops_at_the_run_deadline(monkeypatch, path):
    curl = pytest.importorskip("curl_cffi.requests")
    with _Server() as server, curl.Session() as session:
        batch, elapsed = _read(monkeypatch, lambda: session.get(server.url(path), timeout=60))
    _assert_deadline(batch, elapsed)


@pytest.mark.parametrize("library", ["playwright", "patchright"])
def test_browser_redirects_are_counted_as_requests_and_do_not_stop_the_read(monkeypatch, library):
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
    assert batch["requests"] == 13  # counted as they are, no limit
    assert batch["stop_reason"] == "upstream_exhausted" and batch["error"] is None
    assert batch["upstream_pagination_exhausted"] and len(batch["records"]) == 1


@pytest.mark.parametrize("library", ["playwright", "patchright"])
def test_browser_requests_to_other_hosts_are_aborted_and_not_counted(monkeypatch, library):
    """目标站点是 127.0.0.1：页面自己的 fetch 过闸计数；发往 localhost（别的主机）的"广告"请求直接中止，
    服务器看不到它，也不占请求数。"""
    sync_api = pytest.importorskip(library + ".sync_api")

    def browse():
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, args=["--no-proxy-server"])
            except Exception as exc:  # noqa: BLE001
                pytest.skip("Chromium 不可用：" + type(exc).__name__)
            try:
                browser.new_page().goto(server.url("/page"), wait_until="networkidle", timeout=15000)
            finally:
                browser.close()

    with _Server() as server:
        batch, _ = _read(monkeypatch, browse, deadline=30, url_pattern="127.0.0.1")
    assert sorted(server.hits) == ["/own", "/page"]
    assert batch["requests"] == 2 and batch["error"] is None


def test_on_host_takes_the_host_and_its_subdomains():
    from collection.transport import on_host

    assert on_host("https://www.fda.gov/x", "fda.gov") and on_host("https://fda.gov/x", "fda.gov")
    assert not on_host("https://notfda.gov/x", "fda.gov") and not on_host("https://fda.gov.evil.example/", "fda.gov")
    assert not on_host("data:text/plain,x", "fda.gov")


@pytest.mark.parametrize(("library", "wait"), [
    ("playwright", "selector"), ("playwright", "locator"), ("playwright", "function"), ("playwright", "sleep"),
    ("patchright", "selector"),
])
def test_browser_waits_stop_at_the_run_deadline(monkeypatch, library, wait):
    """等一个永远不出现的选择器（页面默认超时 30 秒）、等 locator、等函数（60 秒）、睡 60 秒：都在期限附近停，
    报 deadline。以前这些等待不受期限约束（scrapling 等选择器的 30 秒，nmpa / fda_drug_safety 因此超期）。"""
    sync_api = pytest.importorskip(library + ".sync_api")

    def browse():
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, args=["--no-proxy-server"])
            except Exception as exc:  # noqa: BLE001
                pytest.skip("Chromium 不可用：" + type(exc).__name__)
            try:
                page = browser.new_page()
                page.goto(server.url("/hop/0"), wait_until="load", timeout=15000)
                waits = {
                    "selector": lambda: page.wait_for_selector("#never"),
                    "locator": lambda: page.locator("#never").first.wait_for(),
                    "function": lambda: page.wait_for_function("() => false", timeout=60000),
                    "sleep": lambda: page.wait_for_timeout(60000),
                }
                waits[wait]()
            finally:
                browser.close()

    with _Server() as server:
        batch, elapsed = _read(monkeypatch, browse)
    _assert_deadline(batch, elapsed)

