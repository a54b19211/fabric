"""一次读取的唯一闸门和预算。

所有读取插件（公开接口、站点插件、本地导入、PubMed 全文）共用这里的 Budget：请求数、字节数、版本数、截止时间，
加上共享限速组（collection.rate，按每一跳的实际目标主机）。读取期间，这个线程（及复制了上下文的线程）里发出的
每一次真实传输都在发出那一刻过闸：

- httpx：Client._send_single_request，每一跳一次（自动跟随的重定向逐跳过闸）；
- requests：HTTPAdapter.send，每一跳一次（requests 的重定向循环逐跳调用它）；
- curl_cffi：Session.request，关掉库内部的自动跳转，由这里逐跳跟随、逐跳过闸；
- 浏览器（playwright 与 patchright，scrapling 的 StealthyFetcher 用后者）：页面上的每个 document / xhr / fetch
  请求在路由处过闸；脚本、样式等子资源计数（subrequests）和计字节，不占请求数、不单独限速。

字节按实际读到的每一块计（含重定向和 429 的响应体、流式下载），用完当场停。超时和 429 在每一跳有界重试，
每次重发都重新过闸、重新计数。预算停止是 BaseException（Stop），拉取函数里的 except Exception 吞不掉；
一旦停下，同一次读取之后的任何传输都直接停。没有活动读取时（API 服务的其他调用、索引客户端等）原样放行。

停在哪里（Budget.check）：每次发出前、每读到一块、每交付一个版本，以及导航返回时。
截止时间：每次等待（连接、读块、整次传输、限速、重试间隔）都不超过开始等待那一刻的剩余时间——读块的超时在每次
阻塞读之前按当时的剩余时间重设（httpx 在 httpcore 的 SyncStream.read，requests 在 http.client 底下的
SocketIO.readinto；响应头、chunk 内部、正文都从这两处读），读块因到期超时报 deadline。
请求数：唯一先发后计的是浏览器内部跟随的重定向（发出前拦不到，Chromium 一次导航最多跟 20 跳）。照实计数，
导航一返回就按 request_budget 停，这一页不交付，不报读到底。
"""

from __future__ import annotations

import contextvars
import functools
import os
import socket
import time
from contextlib import contextmanager
from urllib.parse import urljoin

from collection import rate

_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("collection_budget", default=None)
_MARK = "__wrapped_by_collection__"
_REDIRECTS = {301, 302, 303, 307, 308}
GATED_BROWSER_TYPES = {"document", "xhr", "fetch"}


def byte_budget() -> int:
    return int(os.environ.get("DATAFABRIC_READ_MAX_BYTES", str(256 * 1024 * 1024)))


def _retry_settings():
    from config.settings import APP

    return max(0, int(APP.TASK_RETRIES)), min(max(float(APP.TASK_RETRY_DELAY), 0.0), 60.0)


class Stop(BaseException):
    """预算用完：reason 即 stop_reason。已读到的材料保留，读取插件给出续读位置。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Budget:
    """一次读取的预算与计量。版本数 ≤ max_records（父记录、附件、全文都算）；请求数 ≤ max_pages × 10；
    字节 ≤ DATAFABRIC_READ_MAX_BYTES；截止时间 deadline_seconds。任何一项用完就停，并且停住。"""

    def __init__(self, *, max_requests, max_bytes, max_records, deadline_seconds):
        self.max_requests, self.max_bytes, self.max_records = max_requests, max_bytes, max_records
        self.deadline = time.monotonic() + deadline_seconds
        self.requests = self.bytes = self.records = self.subrequests = self.redirect_hops = 0
        self.stopped: str | None = None

    @classmethod
    def for_request(cls, request):
        return cls(max_requests=request.max_pages * 10, max_bytes=byte_budget(),
                   max_records=request.max_records, deadline_seconds=request.deadline_seconds)

    def stop(self, reason):
        self.stopped = self.stopped or reason
        raise Stop(self.stopped)

    def check(self):
        if self.stopped:
            raise Stop(self.stopped)
        if self.bytes > self.max_bytes:
            self.stop("byte_budget")
        if self.requests > self.max_requests:  # 只有浏览器内部跳转会先发后计
            self.stop("request_budget")
        if time.monotonic() >= self.deadline:
            self.stop("deadline")

    def before_send(self, url):
        self.check()
        if self.bytes >= self.max_bytes:
            self.stop("byte_budget")
        if self.requests >= self.max_requests:
            self.stop("request_budget")
        try:
            rate.wait_for_url(url, deadline=self.deadline)
        except TimeoutError:
            self.stop("deadline")
        self.requests += 1

    def take_bytes(self, n):
        self.bytes += n
        self.check()

    def need_record(self):
        self.check()
        if self.records >= self.max_records:
            self.stop("record_budget")

    def take_record(self):
        self.need_record()
        self.records += 1

    def timeout(self, cap):
        """一次等待的上限：调用方的上限与剩余时间取小。多给 50ms，让被期限截断的等待一定在到期之后才超时，
        失败处的 check 就能认出是 deadline（定时器早到亚毫秒会被当成普通超时）。"""
        return max(0.1, min(cap, self.deadline - time.monotonic() + 0.05))

    def within(self, timeout, default=3600.0):
        """把调用方给的超时（数字、元组或 httpx 的分项字典；None 是不限）收进剩余时间。"""
        if isinstance(timeout, dict):
            return {key: self.within(value) for key, value in timeout.items()}
        if isinstance(timeout, tuple):
            return tuple(self.within(value) for value in timeout)
        return self.timeout(default if timeout is None else timeout)

    def counted(self, chunks):
        """逐块计字节并检查；读块出错时恰因到期（每次套接字读都收在剩余时间里）就报 deadline，否则原样抛出。"""
        try:
            for chunk in chunks:
                self.take_bytes(len(chunk))
                yield chunk
        except Exception:
            self.check()
            raise

    def pause(self, seconds):
        if time.monotonic() + seconds >= self.deadline:
            self.stop("deadline")
        time.sleep(seconds)


def current() -> Budget | None:
    return _ACTIVE.get()


def gate(url):
    """一次真实传输之前（替身和自带传输的代码用）：有活动读取就过闸计数。"""
    budget = _ACTIVE.get()
    if budget is not None:
        budget.before_send(url)


def received(n):
    """自带传输的代码报告实际读到的字节。"""
    budget = _ACTIVE.get()
    if budget is not None:
        budget.take_bytes(n)


def get(client, url, params=None, *, cap, timeout=20.0):
    """一次有界 GET → (状态码, 字节)。闸门和预算由传输层执行；cap 只是这一个响应的大小上限（防内存），
    超过抛 ValueError("response_too_large")。"""
    budget = _ACTIVE.get()
    body = bytearray()
    with client.stream("GET", url, params=params, timeout=budget.timeout(timeout) if budget else timeout) as response:
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > cap:
                raise ValueError("response_too_large")
        return response.status_code, bytes(body)


def is_transient(error: BaseException) -> bool:
    """只重试超时和限流（429）；解析错误和其他 HTTP 错误不重试。"""
    seen = set()
    while isinstance(error, BaseException) and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TimeoutError) or any(
            cls.__name__ in {"TimeoutException", "TimeoutError", "ReadTimeout", "ConnectTimeout",
                             "WriteTimeout", "PoolTimeout", "Timeout"}  # Timeout：curl_cffi
            for cls in type(error).__mro__
        ):
            return True
        if getattr(getattr(error, "response", None), "status_code", None) == 429:
            return True
        error = error.__cause__ or error.__context__
    return False


def _hop(budget, url, send, *, drain, gated=True):
    """一跳：过闸 → 发送；超时和 429 有界重试，每次重发重新过闸。drain 读完并关闭 429 的响应（字节照计）。"""
    retries, delay = _retry_settings()
    for attempt in range(retries + 1):
        if gated:
            budget.before_send(url)
        try:
            response = send()
        except Exception as error:
            budget.check()  # 等待恰因到期而失败（超时收在剩余时间里）：报 deadline，不报来源失败
            if attempt == retries or not is_transient(error):
                raise
            budget.pause(delay)
            delay = min(delay * 2, 60.0)
            continue
        status = getattr(response, "status_code", None) or getattr(response, "status", None)
        if status == 429 and attempt < retries:
            drain(response)
            budget.pause(delay)
            delay = min(delay * 2, 60.0)
            continue
        return response
    raise AssertionError("unreachable")


# —— httpx ——

def _httpx_hop(original):
    import httpx

    class Counted(httpx.SyncByteStream):
        def __init__(self, stream, budget):
            self.stream, self.budget = stream, budget

        def __iter__(self):
            return self.budget.counted(self.stream)

        def close(self):
            self.stream.close()

    def send(self, request):
        budget = _ACTIVE.get()
        if budget is None:
            return original(self, request)

        def once():
            request.extensions["timeout"] = budget.within(request.extensions.get("timeout", {}))
            response = original(self, request)
            if hasattr(response, "_content"):  # 传输层已整块读入（内存响应等）：当场计
                budget.take_bytes(len(response._content))
            else:
                response.stream = Counted(response.stream, budget)
            return response

        def drain(response):
            try:
                response.read()
            finally:
                response.close()

        return _hop(budget, str(request.url), once, drain=drain)

    return send


# —— 套接字读：每一次阻塞读都收进当前剩余时间（读超时是发送时设的，不会随已用时间减少）——

def _httpcore_read(original):
    """httpx 经 httpcore：响应头、chunk、正文的每次 recv 都走 SyncStream.read，按传入的读超时重设套接字。"""

    def read(self, max_bytes, timeout=None):
        budget = _ACTIVE.get()
        return original(self, max_bytes, budget.within(timeout) if budget is not None else timeout)

    return read


def _socketio_readinto(original):
    """requests 经 urllib3 / http.client：响应头、chunk、正文的每次 recv 都走 socket.makefile 底下的 SocketIO.readinto。"""

    def readinto(self, buffer):
        budget = _ACTIVE.get()
        if budget is not None:
            try:
                self._sock.settimeout(budget.within(self._sock.gettimeout()))
            except OSError:  # 套接字已关：交给原读取报错
                pass
        return original(self, buffer)

    return readinto


# —— requests ——

class _CountedRaw:
    def __init__(self, raw, budget):
        self._raw, self._budget = raw, budget

    def stream(self, amt=2**16, decode_content=None):
        if getattr(self._raw, "chunked", False) or not hasattr(self._raw, "read1"):
            chunks = self._raw.stream(amt, decode_content=decode_content)
        else:  # read(amt) 要攒满 amt 才返回，慢速返回时一块能拖过期限；read1 到多少返回多少
            chunks = iter(lambda: self._raw.read1(amt, decode_content=decode_content), b"")
        return self._budget.counted(chunks)

    def read(self, *args, **kwargs):
        try:
            data = self._raw.read(*args, **kwargs)
        except Exception:
            self._budget.check()
            raise
        self._budget.take_bytes(len(data or b""))
        return data

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _requests_hop(original):
    def send(self, request, *args, **kwargs):
        budget = _ACTIVE.get()
        if budget is None:
            return original(self, request, *args, **kwargs)

        def once():
            kwargs["timeout"] = budget.within(kwargs.get("timeout"))
            response = original(self, request, *args, **kwargs)
            response.raw = _CountedRaw(response.raw, budget)
            return response

        def drain(response):
            try:
                response.content  # noqa: B018 -- 读完（计字节）再关
            finally:
                response.close()

        return _hop(budget, request.url, once, drain=drain)

    return send


# —— curl_cffi ——

def _curl_request(original):
    def request(self, method, url, *args, **kwargs):
        budget = _ACTIVE.get()
        if budget is None:
            return original(self, method, url, *args, **kwargs)
        follow = kwargs.pop("allow_redirects", True) is not False
        limit = kwargs.pop("max_redirects", None)
        limit = 10 if limit is None or limit < 0 else min(limit, 10)
        stream = bool(kwargs.get("stream"))
        timeout = kwargs.pop("timeout", getattr(self, "timeout", None))

        def once():
            return original(self, method, url, *args, allow_redirects=False, timeout=budget.within(timeout), **kwargs)

        for _ in range(limit + 1):
            response = _hop(budget, url, once, drain=lambda r: r.close() if stream else budget.take_bytes(len(r.content)))
            if stream:
                chunks = response.iter_content
                response.iter_content = lambda *a, _chunks=chunks, **k: budget.counted(_chunks(*a, **k))
            else:
                budget.take_bytes(len(response.content))
            location = response.headers.get("location")
            if not follow or response.status_code not in _REDIRECTS or not location:
                return response
            url = urljoin(url, location)
            if response.status_code == 303 or (response.status_code in (301, 302) and method.upper() == "POST"):
                method = "GET"
                for key in ("data", "json", "files"):
                    kwargs.pop(key, None)
        raise RuntimeError("too_many_redirects")

    return request


# —— 浏览器 ——

def _route(page):
    """页面上每个请求都经过这里：document / xhr / fetch 过闸，其余计数；然后交给页面原有的路由（如禁用资源）。
    路由回调在浏览器驱动的协程里跑，拿不到读取的上下文变量，所以读取挂在页面上。"""
    if getattr(page, "_collection_routed", False):
        return

    def handle(route, request):
        budget = getattr(page, "_collection_budget", None)
        if budget is not None:
            if request.resource_type in GATED_BROWSER_TYPES:
                try:
                    budget.before_send(request.url)
                except Stop:
                    route.abort()
                    return
            else:
                budget.subrequests += 1
        route.fallback()

    def redirected(request):
        # 浏览器内部跟随的重定向，下一跳不经过路由，发出前拦不住：照实计数（占请求数）；超了之后的请求在路由处中止，
        # 导航返回时 check 按 request_budget 停
        budget = getattr(page, "_collection_budget", None)
        if budget is not None and request.redirected_from is not None and request.resource_type in GATED_BROWSER_TYPES:
            budget.requests += 1
            budget.redirect_hops += 1

    def finished(request):
        budget = getattr(page, "_collection_budget", None)
        if budget is not None:
            size = _browser_body_size(request)  # 先取大小再加：取大小会切到浏览器驱动，期间别的事件也在加
            budget.bytes += size  # 事件里不抛：下次过闸、导航返回或交付版本时 check 停

    page.route("**/*", handle)  # 后注册的路由先执行：先过闸，再交给 scrapling 等原有路由
    page.on("request", redirected)
    page.on("requestfinished", finished)
    page._collection_routed = True


def _browser_body_size(request) -> int:
    """响应体字节。document / xhr / fetch 读实际响应体长度；子资源用浏览器给的 responseBodySize，
    取不到（patchright 等返回 -1）用 content-length。"""
    if request.resource_type in GATED_BROWSER_TYPES:
        try:
            response = request.response()
            if response is not None and not (300 <= response.status < 400):
                return len(response.body())
        except Exception:  # noqa: BLE001 -- 取不到（页面已关闭等）退回浏览器给的大小
            pass
    try:
        size = int(request.sizes().get("responseBodySize") or 0)
    except Exception:  # noqa: BLE001
        size = -1
    if size >= 0:
        return size
    try:
        response = request.response()
        return max(0, int((response.headers if response else {}).get("content-length") or 0))
    except Exception:  # noqa: BLE001
        return 0


def _browser_goto(original):
    def goto(self, url, *args, **kwargs):
        budget = _ACTIVE.get()
        self._collection_budget = budget
        if budget is None:
            return original(self, url, *args, **kwargs)
        _route(self)
        wait = kwargs.get("timeout")  # 毫秒；None 是页面默认（30 秒），0 是不限
        kwargs["timeout"] = budget.within(30.0 if wait is None else (wait / 1000 or None)) * 1000
        try:
            response = _hop(budget, url, lambda: original(self, url, *args, **kwargs),
                            drain=lambda r: None, gated=False)  # 导航请求本身在路由处过闸
        except Exception:
            budget.check()  # 预算停止（路由已中止导航）优先于浏览器报的中止错误
            raise
        budget.check()
        return response

    return goto


def _patch(owner, name, wrap):
    original = getattr(owner, name, None)
    if original is None or getattr(original, _MARK, False):
        return
    wrapped = functools.wraps(original)(wrap(original))
    setattr(wrapped, _MARK, True)
    setattr(owner, name, wrapped)


def install():
    """把闸门装到各传输层的实际发送处。幂等（已包过的带标记）；每次按当前的类检查。没装的库跳过。"""
    from httpx._client import Client as HttpxClient

    from httpcore._backends.sync import SyncStream

    _patch(HttpxClient, "_send_single_request", _httpx_hop)
    _patch(SyncStream, "read", _httpcore_read)
    _patch(socket.SocketIO, "readinto", _socketio_readinto)
    try:
        from requests.adapters import HTTPAdapter

        _patch(HTTPAdapter, "send", _requests_hop)
    except ImportError:
        pass
    try:
        from curl_cffi.requests import Session as CurlSession

        _patch(CurlSession, "request", _curl_request)
    except ImportError:
        pass
    for module in ("playwright.sync_api", "patchright.sync_api"):
        try:
            page = __import__(module, fromlist=["Page"]).Page
        except ImportError:
            continue
        _patch(page, "goto", _browser_goto)


@contextmanager
def metered(budget):
    """这段时间里（这个线程及复制了上下文的线程）的所有真实传输都过 budget。"""
    install()
    token = _ACTIVE.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE.reset(token)


@contextmanager
def reading(request):
    """这次读取的预算：已有活动读取就沿用（read_public 外层建，插件内层复用），否则按请求新建。"""
    active = _ACTIVE.get()
    if active is not None:
        yield active
        return
    with metered(Budget.for_request(request)) as budget:
        yield budget


def in_context(fn):
    """给要交给其他线程执行的函数带上当前上下文（翻页驱动在独立线程里跑浏览器）。"""
    ctx = contextvars.copy_context()
    return functools.partial(ctx.run, fn)
