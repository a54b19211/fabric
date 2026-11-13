"""一个刷新周期读什么：按来源目录（fetch.catalog）里的增量读法，定这一周期的请求。

- 按更新时间窗口读的来源：WHO、GDELT 用 updated_since/updated_until，Europe PMC、ClinicalTrials 把日期条件拼进检索词。
  窗口从"检查点（上一周期读完的终点）减重叠天数"读到现在；还没有检查点时回看 lookback_days 天。
  GDELT 只读走完的整点；读到一半停下时，检查点是第一个没读到的窗口（first_unread）。
- 列表从新到旧的来源（stop_at_known）：每个周期从第一页读起，连续 KNOWN_STOP 条已存且没变就停（reached_known）。
  上一周期有条目读不到（state["read_all"]）时这一周期不这样停，整份读。分组读取的来源（港交所、NCCN）每组各自停，
  每组最多读最新的 GROUP_LIMIT 条（fetch.site_crawler），不用 read_all。
- 明确清单、整份重读（full_reread）的来源：不加窗口，读到底。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fetch.catalog import spec
from store.timestamps import parse_timestamp

KNOWN_STOP = 3  # 连续读到这么多条"已存且没变"的，就当读到旧的了（3 条能容忍置顶的老条目）
SEGMENTS_PER_CYCLE = 5  # stop_at_known 的来源一个周期最多读这么多段：空库第一次不会一直读下去，更老的用 ops/backfill_source.py 补
GROUP_LIMIT = 10  # 分组读取的来源，一个周期每组最多读最新的这么多条


def cycle_request(watch, state: dict, now: datetime) -> tuple[dict, datetime | None, datetime | None]:
    """返回 (这一周期的请求体, 窗口起点, 窗口终点)；没有窗口的来源起止都是 None。"""
    request = watch.request.model_dump(mode="json")
    source, incremental = watch.request.source_id, spec(watch.request.source_id).incremental
    if request["source_record_ids"] or incremental.kind in ("fixed_ids", "full_reread", "stop_at_known"):
        if incremental.kind == "stop_at_known" and not request["source_record_ids"] and (
                incremental.by_group or not state.get("read_all")):
            request["known_stop"] = KNOWN_STOP
        return request, None, None
    checkpoint = state.get("window_checkpoint")
    if checkpoint:
        start = parse_timestamp(checkpoint) - timedelta(days=incremental.overlap_days)
    else:
        start = now - timedelta(days=watch.lookback_days)
    end = now
    if source == "gdelt_doc":  # 只读走完的整点；还没有新的整点就重读最近一个，不出空窗口
        end = _hour(now)
        start = start if checkpoint else _hour(start)
        start = start if start < end else end - timedelta(hours=1)
    first, last = start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()
    if source in ("who_don", "gdelt_doc"):
        request.update(updated_since=start.isoformat(), updated_until=end.isoformat())
    elif source == "europe_pmc":
        request["query"] = f"({request['query']}) AND UPDATE_DATE:[{first} TO {last}]"
    elif source == "clinicaltrials":
        dates = f"AREA[LastUpdatePostDate]RANGE[{first},{last}]"
        request["query"] = dates if request["query"] == "*" else f"({request['query']}) AND {dates}"
    else:
        raise ValueError("no_window_builder:" + source)
    return request, start, end


def _hour(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def first_unread(coverage: dict) -> str | None:
    """按窗口读的读取回执 → 最早一个没读到（失败或没轮到）的窗口的起点；每个窗口都读到了是 None。"""
    starts = [parse_timestamp(receipt["start"]) for receipt in coverage["window_coverage"]
              if receipt["status"] in ("failed", "not_read") and receipt["start"]]
    return min(starts).isoformat() if starts else None
