"""增量窗口：按来源目录里声明的增量键，生成一个刷新周期的固定读取范围。

持久窗口检查点：一个实时周期读完（来源分页确认到底）后，把本周期窗口终点记为检查点；
下一周期从“检查点 − 重叠天数”读起。停机多久就补多久，不会因为回看天数固定而漏掉停机期间的更新。
没有检查点（首次运行或旧状态）时按 lookback_days 回看，与改造前行为一致。

回填模式不加时间窗，按来源自己的续页标记读完整个范围；读到底后按周期重新对账。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from collection.catalog import spec


def window_start(watch, state: dict, now: datetime) -> datetime:
    checkpoint = state.get("window_checkpoint")
    overlap = timedelta(days=spec(watch.request.source_id).incremental.overlap_days)
    if checkpoint:
        return datetime.fromisoformat(checkpoint) - overlap
    return now - timedelta(days=watch.lookback_days)


def cycle_request(watch, state: dict, now: datetime) -> tuple[dict, datetime | None, datetime | None]:
    """返回 (请求体, 窗口起点, 窗口终点)。回填和明确清单没有窗口，起止为 None。"""
    request = watch.request.model_dump(mode="json")
    kind = spec(watch.request.source_id).incremental.kind
    if watch.mode == "backfill" or kind == "fixed_ids" or request.get("source_record_ids"):
        return request, None, None
    start, end = window_start(watch, state, now), now
    source = watch.request.source_id
    if kind == "modified_window":
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
