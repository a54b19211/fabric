"""共享限速组：同一站点（或同一出口）的所有采集任务共用一份请求间隔。

用 Redis 的 SET NX PX 做跨进程闸门：拿到键的请求才能发，键的寿命就是最小间隔。
Redis 未配置或暂时不可用时，退回进程内间隔——只会更慢，不会更快。
"""

from __future__ import annotations

import os
import threading
import time
from functools import lru_cache

_local_lock = threading.Lock()
_local_next: dict[str, float] = {}


@lru_cache(maxsize=1)
def _redis():
    url = os.environ.get("DATAFABRIC_HISTORY_REDIS_URL", "")
    if not url:
        return None
    from redis import Redis

    return Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)


def _wait_local(group: str, interval: float) -> None:
    with _local_lock:
        now = time.monotonic()
        start = max(now, _local_next.get(group, 0.0))
        _local_next[group] = start + interval
    if start > now:
        time.sleep(start - now)


def wait(group: str, interval: float, *, deadline: float | None = None) -> None:
    """阻塞到本组可以发下一个请求。deadline 是 time.monotonic() 的绝对值，过了就抛 TimeoutError。"""
    if interval <= 0:
        return
    client = _redis()
    if client is None:
        _wait_local(group, interval)
        return
    key = "datafabric:rate:" + group
    ms = max(1, int(interval * 1000))
    while True:
        try:
            if client.set(key, b"1", nx=True, px=ms):
                return
            left = client.pttl(key)
        except Exception:  # noqa: BLE001 -- 闸门故障时退回本地间隔，宁慢勿快
            _wait_local(group, interval)
            return
        pause = (left if isinstance(left, int) and left > 0 else ms) / 1000
        if deadline is not None and time.monotonic() + pause > deadline:
            raise TimeoutError(group)
        time.sleep(pause)
