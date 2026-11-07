"""共享限速组：同一站点（或同一出口）的所有采集请求共用一份请求间隔。每个对外 HTTP 请求发出前都过闸。

- 首选 Redis（SET NX PX）：跨进程、跨主机。
- 没配置 Redis（明确的单机模式）：用本机文件锁闸门（fcntl.flock + 共享的“下一次可发时间”）。它跨进程，
  同一主机上所有采集进程共用一份间隔（每进程各自计时的话，3 个进程下实际速率放大 3 倍）。
- 配置了 Redis 但它暂时故障：不切到文件闸门——那样会和仍连得上 Redis 的进程分成两套时钟（实测
  两次放行只隔 0.0006 秒）。而是退避重试，等到 deadline 仍不行就抛 TimeoutError，调用方按截止停下，不发请求。
  跨主机部署必须配 Redis。
- 等待受 deadline 约束（time.monotonic() 的绝对值），等不到就抛 TimeoutError，调用方记为截止。

Outbound counters (take_for_url): a site that declares an OutboundCap in the catalog also has its requests counted per
UTC day, and per UTC hour for each value of the `query` parameter. The counters sit in the same place as the
interval gate (Redis with an expiry, or one lock-guarded file per group), so every read and every process of this
instance adds to the same count. A request that would pass a ceiling is refused and not counted. The counters are
this instance's own ceiling, not a limit shared with other instances; a flushed Redis or an emptied DATAFABRIC_RATE_DIR
starts the count again.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

REDIS_FAILURE_RETRIES = 8
HOUR, DAY = 3600, 86400


@lru_cache(maxsize=1)
def _redis():
    url = os.environ.get("DATAFABRIC_HISTORY_REDIS_URL", "")
    if not url:
        return None
    from redis import Redis

    return Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)


def _gate_dir() -> Path:
    path = Path(os.environ.get("DATAFABRIC_RATE_DIR", "/tmp/datafabric-rate"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _wait_file(group: str, interval: float, deadline: float | None) -> None:
    path = _gate_dir() / (re.sub(r"[^A-Za-z0-9._-]", "_", group) + ".gate")
    with open(path, "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.seek(0)
            text = stream.read().strip()
            try:
                next_at = float(text) if text else 0.0
            except ValueError:
                next_at = 0.0
            now = time.time()
            start = max(now, next_at)
            if deadline is not None and time.monotonic() + (start - now) > deadline:
                raise TimeoutError(group)
            stream.seek(0)
            stream.truncate()
            stream.write(repr(start + interval))
            stream.flush()
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
    if start > now:
        time.sleep(start - now)


def wait(group: str, interval: float, *, deadline: float | None = None) -> None:
    """阻塞到本组可以发下一个请求。"""
    if interval <= 0:
        return
    client = _redis()
    if client is None:
        _wait_file(group, interval, deadline)
        return
    key = "datafabric:rate:" + group
    ms = max(1, int(interval * 1000))
    failures = 0
    while True:
        try:
            if client.set(key, b"1", nx=True, px=ms):
                return
            left = client.pttl(key)
        except Exception:  # noqa: BLE001 -- Redis 故障：不发请求，退避重试
            failures += 1
            if deadline is None and failures >= REDIS_FAILURE_RETRIES:
                raise TimeoutError("rate_gate_unavailable:" + group) from None
            left = int(min(5.0, 0.25 * 2 ** failures) * 1000)
        pause = (left if isinstance(left, int) and left > 0 else ms) / 1000
        if deadline is not None and time.monotonic() + pause > deadline:
            raise TimeoutError(group)
        time.sleep(pause)


def group_for_url(url: str):
    """URL 所属的限速组（来源目录里的 rate_group 是主机名后缀）。不认识的主机返回 None。"""
    from collection.catalog import SPECS

    host = (urlparse(url).hostname or "").lower()
    best = None
    for spec in SPECS:
        group = spec.rate_group.lower()
        if host == group or host.endswith("." + group):
            if best is None or len(group) > len(best.rate_group):
                best = spec
    return best


def wait_for_url(url: str, *, deadline: float | None = None, default_interval: float = 1.0) -> None:
    """按 URL 的主机过闸；目录里没有的主机用自己的主机名作组、默认 1 秒间隔，不放行无限速请求。"""
    spec = group_for_url(url)
    if spec is not None:
        wait(spec.rate_group, spec.min_interval_seconds, deadline=deadline)
    else:
        wait((urlparse(url).hostname or "unknown").lower(), default_interval, deadline=deadline)


class _Ceiling(NamedTuple):
    reason: str  # stop reason once the ceiling is reached
    key: str  # what is counted inside the period
    limit: int
    period: int  # seconds; counted per UTC period, so an hour or a day starts at its mark


def _ceilings(url: str) -> tuple[str, list[_Ceiling]]:
    """(rate group, ceilings) the catalog declares for the url's site; no ceilings for a site without a cap."""
    declared = group_for_url(url)
    cap = declared.outbound_cap if declared else None
    if cap is None:
        return "", []
    ceilings = [_Ceiling("outbound_quota_day", "all", cap.per_day, DAY)]
    if cap.per_hour_per_query:
        query = parse_qs(urlparse(url).query).get("query", [""])[0]
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        ceilings.append(_Ceiling("outbound_quota_hour", "query:" + digest, cap.per_hour_per_query, HOUR))
    return declared.rate_group, ceilings


def _bucket(ceiling: _Ceiling, now: float) -> str:
    return f"{ceiling.key}|{ceiling.period}|{int(now // ceiling.period)}"


def _current(bucket: str, now: float) -> bool:
    _, period, index = bucket.rsplit("|", 2)
    return int(index) == int(now // int(period))


def _take_file(group: str, ceilings: list[_Ceiling], now: float) -> str | None:
    path = _gate_dir() / (re.sub(r"[^A-Za-z0-9._-]", "_", group) + ".quota")
    with open(path, "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.seek(0)
            try:
                saved = json.loads(stream.read() or "{}")
            except ValueError:
                saved = {}
            counts = {k: v for k, v in saved.items() if _current(k, now)} if isinstance(saved, dict) else {}
            buckets = [_bucket(ceiling, now) for ceiling in ceilings]
            full = next((c.reason for c, b in zip(ceilings, buckets) if counts.get(b, 0) >= c.limit), None)
            if full is None:
                for bucket in buckets:
                    counts[bucket] = counts.get(bucket, 0) + 1
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(counts))
            stream.flush()
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
    return full


def _take_redis(client, group: str, ceilings: list[_Ceiling], now: float) -> str | None:
    keys = ["datafabric:quota:" + group + ":" + _bucket(ceiling, now) for ceiling in ceilings]
    try:
        pipe = client.pipeline()
        for key, ceiling in zip(keys, ceilings):
            pipe.incr(key)
            pipe.expire(key, ceiling.period * 2)
        counts = pipe.execute()[::2]
        full = next((c.reason for c, n in zip(ceilings, counts) if n > c.limit), None)
        if full is not None:
            undo = client.pipeline()
            for key in keys:
                undo.decr(key)
            undo.execute()
    except Exception:  # noqa: BLE001 -- Redis 故障：数不了就不发请求，和限速闸门同一做法
        raise TimeoutError("rate_gate_unavailable:" + group) from None
    return full


def take_for_url(url: str) -> str | None:
    """Count one request to the url against its site's outbound cap. The stop reason when a ceiling is already
    reached (outbound_quota_day or outbound_quota_hour): that request is not counted and must not be sent. None when
    it may go, or when the site declares no cap. Raises TimeoutError when the counters cannot be reached."""
    group, ceilings = _ceilings(url)
    if not ceilings:
        return None
    client, now = _redis(), time.time()
    return _take_file(group, ceilings, now) if client is None else _take_redis(client, group, ceilings, now)
