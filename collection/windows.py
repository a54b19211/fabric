"""增量窗口：按来源目录里声明的增量键，生成一个刷新周期的固定读取范围。

持久窗口检查点：一个实时周期读完（来源分页确认到底）后，把本周期窗口终点记为检查点；
下一周期从“检查点 − 重叠天数”读起。停机多久就补多久，不会因为回看天数固定而漏掉停机期间的更新。
没有检查点（首次运行或旧状态）时按 lookback_days 回看，与改造前行为一致。

回填模式不加时间窗，按来源自己的续页标记读完整个范围；读到底后按周期重新对账。

没有更新时间可过滤、但列表从新到旧的来源（目录里的 stop_at_known）也没有窗口：每个周期从列表第一页读起，连续读到
KNOWN_STOP 条库里已有且没变的就停（读到旧的了）。上一周期有条目读不到（db.source_refresh 的 read_all）时，这一周期不许这样停，
从头读满 SEGMENTS_PER_CYCLE 段。列表不大、旧条目会后补进来的来源（full_reread，如 fda_crl）也没有窗口，每个周期整份重读，不加 known_stop。

GDELT 没有续页，窗口读取按整点：一个周期读 [检查点, 当前整点)，不读还没走完的这一小时，也不回看重叠。
读取没读完时（预算或出站额度用完）从第一个没读到的窗口起点继续（first_unread）；读到了但最细窗口仍满额的区间
重读也只会得到同样被截断的结果，不重试，留在读取回执里（window_coverage、truncated_intervals）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from biz_rules.timestamps import parse_timestamp
from collection.catalog import spec

KNOWN_STOP = 3  # 连续读到这么多条“已存且没变”的，就当读到旧的了（老 ETL 是 1 条；3 条能容忍置顶的老条目）
SEGMENTS_PER_CYCLE = 5  # “遇到已存的就停”的来源，一个刷新周期最多读这么多段（每段最多 max_records 条）；更老的交给回填


def window_start(watch, state: dict, now: datetime) -> datetime:
    checkpoint = state.get("window_checkpoint")
    overlap = timedelta(days=spec(watch.request.source_id).incremental.overlap_days)
    if checkpoint:
        return parse_timestamp(checkpoint) - overlap
    return now - timedelta(days=watch.lookback_days)


def _hour_mark(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _whole_hours(watch, state: dict, now: datetime) -> tuple[datetime, datetime]:
    """[检查点, 当前整点)。没有检查点时从回看起点所在的整点读起。还没有新的整点时重读最近一个整点，
    不产生空窗口或倒置的窗口。"""
    end = _hour_mark(now)
    start = window_start(watch, state, now)
    if not state.get("window_checkpoint"):
        start = _hour_mark(start)
    return (start if start < end else end - timedelta(hours=1)), end


def first_unread(coverage: dict) -> str | None:
    """A windowed read's coverage -> the start of its earliest window that was not read (a stop or a failure left
    it), or None when every window was read."""
    starts = [
        parse_timestamp(receipt["start"])
        for receipt in coverage["window_coverage"]
        if receipt["status"] in {"failed", "not_read"} and receipt["start"]
    ]
    return min(starts).isoformat() if starts else None


def cycle_request(watch, state: dict, now: datetime) -> tuple[dict, datetime | None, datetime | None]:
    """返回 (请求体, 窗口起点, 窗口终点)。回填和明确清单没有窗口，起止为 None。"""
    request = watch.request.model_dump(mode="json")
    kind = spec(watch.request.source_id).incremental.kind
    # 读明确清单（source_record_ids）本来就是读完这份清单，不加；上一周期有条目读不到（read_all）：这一周期整份读，不在“遇到已存的”就停
    if (kind == "stop_at_known" and watch.mode == "realtime" and not request.get("source_record_ids")
            and not state.get("read_all")):
        request["known_stop"] = KNOWN_STOP
    if watch.mode == "backfill" or kind in ("fixed_ids", "stop_at_known", "full_reread") or request.get("source_record_ids"):
        return request, None, None
    source = watch.request.source_id
    if source == "gdelt_doc":
        start, end = _whole_hours(watch, state, now)
    else:
        start, end = window_start(watch, state, now), now
    if kind == "modified_window" or source == "gdelt_doc":
        request.update(updated_since=start.isoformat(), updated_until=end.isoformat())
    elif source == "europe_pmc":
        first = start.astimezone(timezone.utc).date().isoformat()
        last = end.astimezone(timezone.utc).date().isoformat()
        request["query"] = "(" + request["query"] + ") AND UPDATE_DATE:[" + first + " TO " + last + "]"
    elif source == "clinicaltrials":
        date_filter = (
            "AREA[LastUpdatePostDate]RANGE[" + start.date().isoformat() + "," + end.date().isoformat() + "]"
        )
        request["query"] = (
            date_filter if request["query"] == "*" else "(" + request["query"] + ") AND " + date_filter
        )
    else:
        raise ValueError("no_window_builder:" + source)
    return request, start, end
