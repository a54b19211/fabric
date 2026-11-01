"""统一异常捕获器 —— 所有异常通过此类记录，方便统一检索。"""
import functools
import traceback
from typing import Optional
from loguru import logger


class ExceptionLogger:
    """
    用法1（上下文管理器）：
        with ExceptionLogger("[Extraction] 抽取任务"):
            do_work()

    用法2（装饰器）：
        @ExceptionLogger.wrap("[Crawl] 页面抓取")
        def fetch_page(url): ...

    用法3（手动调用，用于已有 except 块的补充）：
        ExceptionLogger.log("[Sync] 同步失败", extra={"uuid": uuid})
    """

    def __init__(self, tag: str, reraise: bool = True, log_level: str = "ERROR", extra: Optional[dict] = None):
        self.tag = tag
        self.reraise = reraise
        self.log_level = log_level
        self.extra = extra or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            self._log(exc_val)
            if not self.reraise:
                return True  # 吞掉异常

    @classmethod
    def wrap(cls, tag: str, reraise: bool = True, log_level: str = "ERROR"):
        """装饰器：自动捕获并记录函数内的异常。"""
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    cls._log_static(tag, e)
                    if reraise:
                        raise
                    return None
            return wrapper
        return decorator

    @staticmethod
    def log(tag: str, exc: Optional[Exception] = None, extra: Optional[dict] = None):
        """手动记录已知异常。"""
        ExceptionLogger._log_static(tag, exc, extra)

    # ── 内部 ──

    def _log(self, exc: Exception):
        self._log_static(self.tag, exc, self.extra)

    @staticmethod
    def _log_static(tag: str, exc: Optional[Exception], extra: Optional[dict] = None):
        tb = traceback.format_exc() if exc else traceback.format_exc()
        parts = [f"[EXCEPTION] {tag}", f"type={type(exc).__name__}" if exc else "", f"msg={exc}", f"traceback=\n{tb}"]
        if extra:
            parts.append(f"extra={extra}")
        logger.opt(depth=2).error(" | ".join(p for p in parts if p))
