"""按站点限速：同一限速组（来源目录里的 rate_group，主机名后缀）的两个请求之间至少隔 min_interval_seconds 秒。

这台机器上的所有采集进程（几个 RQ worker、命令行）共用同一份“下一次可以发的时间”：每个限速组一个文件，放在
DATAFABRIC_RATE_DIR 下，读写时加文件锁（fcntl.flock），排着队取号。几个进程一起抓同一个站，加起来也不超过这个速率。
目录里没有的主机按自己的主机名成组，间隔 1 秒。
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_INTERVAL = 1.0  # 目录里没有的主机


def _gate_dir() -> Path:
    path = Path(os.environ.get("DATAFABRIC_RATE_DIR", "/tmp/datafabric-rate"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def wait(group: str, interval: float) -> None:
    """阻塞到本组可以发下一个请求：取号（下一次可发时间往后推 interval），再睡到号上的时间。"""
    if interval <= 0:
        return
    path = _gate_dir() / (re.sub(r"[^A-Za-z0-9._-]", "_", group) + ".gate")
    with open(path, "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)  # 关文件时自动解锁
        stream.seek(0)
        try:
            next_at = float(stream.read().strip() or 0)
        except ValueError:
            next_at = 0.0
        now = time.time()
        start = max(now, next_at)
        stream.seek(0)
        stream.truncate()
        stream.write(repr(start + interval))
    if start > now:
        time.sleep(start - now)


def group_for_url(url: str):
    """URL 所属的来源声明（来源目录里的 rate_group 是主机名后缀，最长的后缀优先）。不认识的主机返回 None。"""
    from fetch.catalog import SPECS

    host = (urlparse(url).hostname or "").lower()
    best = None
    for spec in SPECS:
        group = spec.rate_group.lower()
        if host == group or host.endswith("." + group):
            if best is None or len(group) > len(best.rate_group):
                best = spec
    return best


def wait_for_url(url: str) -> None:
    """按 URL 的主机等限速。"""
    spec = group_for_url(url)
    if spec is not None:
        wait(spec.rate_group, spec.min_interval_seconds)
    else:
        wait((urlparse(url).hostname or "unknown").lower(), DEFAULT_INTERVAL)
