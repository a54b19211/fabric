"""所有真实传输的统一闸门（T-28 ③，Codex r69）。

一次读取（collection.site_crawler 的 _Reader）期间，这个线程（及它派生、复制了上下文的线程）里发出的
每一次真实传输——httpx / requests / curl_cffi 请求、Playwright 页面跳转和点击翻页——发出前都先过
reader.before_request()：共享限速组（collection.rate）、请求数、字节数、截止时间预算，任何一项用完就停
（抛预算停止，BaseException，拉取函数里的 except Exception 吞不掉）。超时和 429 在这一层有界重试，
每次重发都重新过闸、重新计数；续读时从头重放的请求也照样计数。

没有活动读取时（API 服务里的其他调用、索引客户端等）原样放行，不受影响。
"""

from __future__ import annotations

import contextvars
import functools
import re
import time
from contextlib import contextmanager

_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("collection_reader", default=None)


def is_transient(error: BaseException) -> bool:
    """只重试超时和限流（429）；解析错误和其他 HTTP 错误不重试。"""
    seen = set()
    while isinstance(error, BaseException) and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TimeoutError) or any(
            cls.__name__ in {"TimeoutException", "TimeoutError", "ReadTimeout", "ConnectTimeout",
                             "WriteTimeout", "PoolTimeout"}
            for cls in type(error).__mro__
        ):
            return True
        if getattr(getattr(error, "response", None), "status_code", None) == 429:
            return True
        if isinstance(error, RuntimeError) and re.search(r"\bHTTP\s+429\b", str(error)):
            return True
        error = error.__cause__ or error.__context__
    return False


def _retry_settings():
    from config.settings import APP

    return max(0, int(APP.TASK_RETRIES)), min(max(float(APP.TASK_RETRY_DELAY), 0.0), 60.0)


def gate():
    """一次真实传输之前调用：有活动读取就过闸计数，没有就什么都不做。返回活动读取（或 None）。"""
    reader = _ACTIVE.get()
    if reader is not None:
        reader.before_request()
    return reader


def _status(response):
    return getattr(response, "status_code", None) or getattr(response, "status", None)


def _size(response, streamed):
    if streamed:
        return 0  # 流式下载由调用方按实际写入的字节计（附件按落盘大小计入）
    try:
        return len(response.content)
    except Exception:  # noqa: BLE001 -- 浏览器响应等没有 content 属性
        return 0


def _metered(original, *, streamed_kw="stream", retry_status=True, count_bytes=True):
    @functools.wraps(original)
    def call(*args, **kwargs):
        if _ACTIVE.get() is None:
            return original(*args, **kwargs)
        retries, delay = _retry_settings()
        for attempt in range(retries + 1):
            reader = gate()
            try:
                response = original(*args, **kwargs)
            except BaseException as error:
                if attempt == retries or not isinstance(error, Exception) or not is_transient(error):
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            if retry_status and _status(response) == 429 and attempt < retries:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            add = getattr(reader, "add_bytes", None)
            if add and count_bytes:
                add(_size(response, bool(kwargs.get(streamed_kw))))
            return response
        raise AssertionError("unreachable")

    call.__wrapped_by_collection__ = True
    return call


def _patch(owner, name, **kw):
    original = getattr(owner, name, None)
    if original is None or getattr(original, "__wrapped_by_collection__", False):
        return
    setattr(owner, name, _metered(original, **kw))


def install():
    """把闸门装到各传输层。幂等：已包过的方法带标记不重复包；每次都按当前的类检查，
    中途被替换过的类（测试替身等）恢复后也会重新装上。没装的库跳过。"""
    from httpx._client import Client as HttpxClient  # 类本身（模块属性 httpx.Client 可能被替换）

    _patch(HttpxClient, "send")
    try:
        from requests.sessions import Session as RequestsSession

        _patch(RequestsSession, "send")
    except ImportError:
        pass
    try:
        from curl_cffi.requests import Session as CurlSession

        _patch(CurlSession, "request", count_bytes=False)  # 网页抓取：页面字节由 site_crawler 按原件计
    except ImportError:
        pass
    try:
        from playwright.sync_api import ElementHandle, Locator, Page

        _patch(Page, "goto", retry_status=True, count_bytes=False)
        _patch(ElementHandle, "click", retry_status=False, count_bytes=False)
        _patch(Locator, "click", retry_status=False, count_bytes=False)
    except ImportError:
        pass


@contextmanager
def metered(reader):
    """这次读取期间的所有传输都过 reader 的闸门和预算。"""
    install()
    token = _ACTIVE.set(reader)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def in_context(fn):
    """给要交给其他线程执行的函数带上当前上下文（翻页驱动在独立线程里跑 Playwright）。"""
    ctx = contextvars.copy_context()
    return functools.partial(ctx.run, fn)
