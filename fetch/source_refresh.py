"""定时刷新：DATAFABRIC_HISTORY_WATCHES 里配置的每个固定刷新（watch）到点就去抓，一段一段读完一个周期。

常驻派活程序 run/scheduler.py 每隔几秒调一次 refresh_once()，每个到点的 watch 走一步（_step）：
  1. 没有进行中的周期：按 fetch/windows.py 的 cycle_request 定这一周期的请求（更新时间窗口，或"遇到已存的就停"）。
  2. 没有进行中的任务：建一条采集任务（store.history.create_fetch），放进队列（fetch.fetch_job.enqueue_fetch），
     由采集工人去读。任务还没读完：下一轮再看。
  3. 任务读完了（_segment_done）：有续读位置就下一轮读下一段；读到底、读到已存的、或读够 SEGMENTS_PER_CYCLE 段就收工，
     记下窗口检查点，cadence_seconds 秒后开始下一周期。
  出错（_failed）：cadence_seconds 秒后换一个新任务重读这一段；连续 GIVE_UP_AFTER 次就放弃这一周期。
每个 watch 的状态是 publication_outbox 里 kind='control'、event_key 为 _key(watch) 的那一行的 snapshot（一个 JSON）。
几个派活程序同时跑时，一步在锁住这一行的事务里走（SELECT ... FOR UPDATE SKIP LOCKED），别人锁着就跳过，不会重复派活。
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

from pydantic import Field, model_validator
from sqlalchemy import text

from fetch import fetch_job, transport
from fetch.catalog import WATCHABLE, spec
from fetch.windows import SEGMENTS_PER_CYCLE, cycle_request, first_unread
from graph.publish import publication
from store import history
from store.history_models import FetchRequest, HistoryError, Model, canonical, sha256
from store.timestamps import parse_timestamp

GIVE_UP_AFTER = 3  # 同一周期连续失败这么多次，放弃这一周期


class SourceWatch(Model):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,60}$")
    request: FetchRequest
    cadence_seconds: int = Field(default=3600, gt=0, le=604800)
    lookback_days: int = Field(default=30, ge=1, le=3650)  # 还没有窗口检查点时回看几天

    @model_validator(mode="after")
    def fits_the_source(self):
        source, request = self.request.source_id, self.request
        if source not in WATCHABLE:
            raise ValueError("只有来源目录里已接通、有增量读法的来源能定时刷新")
        if self.cadence_seconds < spec(source).incremental.min_cadence_seconds:
            raise ValueError("刷新间隔短于该来源目录声明的下限")
        if request.continuation or request.updated_since or request.updated_until or request.known_stop:
            raise ValueError("续读位置、更新窗口和 known_stop 由每个周期自己填，配置里不写")
        if source in ("europe_pmc", "clinicaltrials") and len(request.query) > 400:
            raise ValueError("论文/临床的检索词要给更新日期条件留出长度")
        ids = request.source_record_ids
        if source == "sec_companyfacts" and (request.query != "*" or not ids
                                             or len(ids) > min(request.max_pages, request.max_records)):
            raise ValueError("SEC 要给明确的概念清单，页数和条数预算要够读完清单")
        return self


def watches():
    """配置里的固定刷新。配置不对报错，一个也不刷新。"""
    try:
        config = json.loads(os.environ.get("DATAFABRIC_HISTORY_WATCHES", "[]"))
        values = [SourceWatch.model_validate(value) for value in config]
    except (ValueError, TypeError):
        raise HistoryError("source_watch_configuration", "固定刷新配置无效") from None
    if len({value.id for value in values}) != len(values):
        raise HistoryError("source_watch_configuration", "固定刷新的 id 重复")
    return values


def _key(watch):
    """状态行的键：配置（补齐默认值）的 sha256。和改造前同一算法，已在跑的 watch 接着用原来的状态；改了配置就是新 watch。"""
    data = watch.model_dump(mode="json")
    data["request"].pop("known_stop")  # 后来才有的字段（配置里总是 0），不改动已有的键
    return "source-watch:" + sha256(canonical(data))


def refresh_once(engine=None):
    """每个到点的 watch 走一步，返回这一轮走了的 [{"watch_id", "state"}]。没有配置时什么都不做。"""
    configured = watches()
    if not configured:
        return []
    engine = engine or publication.publication_engine()
    results = []
    for watch in configured:
        key = _key(watch)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO publication_outbox (event_key, kind, source_doc_uuid, snapshot) "
                              "VALUES (:key, 'control', :key, '{}') ON CONFLICT (event_key) DO NOTHING"), {"key": key})
        with engine.begin() as conn:
            row = conn.execute(text("SELECT snapshot, clock_timestamp() AS now FROM publication_outbox "
                                    "WHERE event_key = :key FOR UPDATE SKIP LOCKED"), {"key": key}).first()
            if row is None:  # 另一个派活程序正在走这一步
                continue
            state, now = dict(row.snapshot), row.now
            waiting = state.get("state") in ("idle", "failed", "blocked") and state.get("next_check_at")
            if waiting and parse_timestamp(state["next_check_at"]) > now:
                continue
            _step(watch, state, now)
            conn.execute(text("UPDATE publication_outbox SET snapshot = CAST(:state AS jsonb) WHERE event_key = :key"),
                         {"key": key, "state": canonical(state)})
        results.append({"watch_id": watch.id, "state": state["state"]})
    return results


def _step(watch, state, now):
    """这个 watch 的一步：定周期、建任务或看任务、处理读完的一段。出错记进 state（_failed）。"""
    try:
        if "request" not in state:  # 新的一个周期
            request, start, end = cycle_request(watch, state, now)
            state.update(watch_id=watch.id, request=request, segments=0, read_all=False,
                         cycle_started_at=now.isoformat(), window_started_at=start.isoformat() if start else None,
                         window_ended_at=end.isoformat() if end else None)
        if state.get("active_run_id"):
            run = history.read_run(state["active_run_id"])
        else:
            if publication.disk_status()["backfill_paused"]:
                raise HistoryError("source_watch_disk_low", "磁盘低水位，暂停新的采集", 503)
            request = {**state["request"], "known_stop": 0} if state.get("retry") else state["request"]
            run, _ = history.create_fetch(FetchRequest.model_validate(request))
            state["active_run_id"] = run.id
        if run.state in ("queued", "running"):
            if (now - run.created_at).total_seconds() > 3 * fetch_job.JOB_TIMEOUT:  # 采集工人中途死了，任务停在那里
                history.fail_job(run.id, run.attempt, "run_stalled", "采集任务长时间没有结束")
                raise HistoryError("source_watch_failed", "采集任务长时间没有结束", 503)
            if run.state == "queued":
                fetch_job.enqueue_fetch(run)  # 已经在队列里的不会再放一次
            state["state"] = "running"
            return
        # items_unread：读到底了，只是有几条读不到（下一周期整份读，见 _segment_done），不算这一段失败
        unread_only = run.coverage.get("stop_reason") == "items_unread"
        if run.state not in ("completed", "partial") or (run.error and not unread_only):
            raise HistoryError("source_watch_failed", "这一段读取失败", 503)
        state.pop("active_run_id")
        _segment_done(watch, state, run.coverage, now)
    except HistoryError as exc:
        _failed(watch, state, exc, now)


def _segment_done(watch, state, coverage, now):
    """读完的一段：还有下一段就接着读，否则这一周期收工。"""
    request, after = state["request"], coverage.get("next_continuation")
    stop = coverage.get("stop_reason")
    ended = coverage.get("upstream_pagination_exhausted") or stop in (transport.KNOWN, "items_unread")
    if after and after["cursor"] == (request.get("continuation") or {}).get("cursor"):
        raise HistoryError("source_watch_no_progress", "续读位置没有前进", 503)
    if not (after or ended or "window_coverage" in coverage):
        raise HistoryError("source_watch_no_cursor", "来源提前停下，又没给续读位置", 503)
    state.update(segments=state.get("segments", 0) + 1, fail_streak=0, retry=False, error=None,
                 unclean=state.get("unclean", False) or bool(coverage.get("unread_items")))
    incremental = spec(watch.request.source_id).incremental
    newest_first = incremental.kind == "stop_at_known" and not incremental.by_group
    enough = newest_first and state["segments"] >= SEGMENTS_PER_CYCLE
    if after and not enough:
        request["continuation"] = after
        state["state"] = "running"
        return
    gap = None if after or ended else first_unread(coverage)  # GDELT 读到一半停下：从第一个没读到的窗口接着读
    if gap or state.get("window_ended_at"):
        state["window_checkpoint"] = gap or state["window_ended_at"]
    if gap is None:
        state["last_success_at"] = now.isoformat()
    # 这一周期"遇到已存的就停"了，又有条目读不到：它们可能排在已存的后面，下一周期整份读一次
    state["read_all"] = state.pop("unclean") and bool(request.get("known_stop"))
    state.update(state="idle", next_check_at=(now + timedelta(seconds=watch.cadence_seconds)).isoformat())
    del state["request"]


def _failed(watch, state, exc, now):
    """出错：cadence_seconds 秒后换一个新任务重读这一段。重读时不在已存的上停（retry）：前面几条可能是失败的这次存下的，
    停在它们上面就跳过了后面没存的。连续 GIVE_UP_AFTER 次放弃这一周期：下一周期从检查点重新规划，"遇到已存的就停"的整份读。"""
    state.pop("active_run_id", None)
    later = now + timedelta(seconds=watch.cadence_seconds)
    state.update(state="blocked" if exc.status == 403 else "failed", error={"code": exc.code, "message": exc.message},
                 last_failure_at=now.isoformat(), next_check_at=later.isoformat(), retry=True,
                 fail_streak=state.get("fail_streak", 0) + 1)
    if state["fail_streak"] >= GIVE_UP_AFTER:
        for name in ("request", "segments", "unclean", "retry", "fail_streak"):
            state.pop(name, None)
        state["read_all"] = True


WATCH_FIELDS = ("state", "window_started_at", "window_ended_at", "window_checkpoint", "next_check_at",
                "last_success_at", "last_failure_at", "active_run_id", "error")


def source_health(source_id, engine=None):
    """GET /api/history/source-health：这个来源各固定刷新的状态。雷达读 collection 的 state、last_recorded_success_at、
    last_recorded_failure_at、scope、coverage，和 watches[] 的 watch_id、scope、window_ended_at、state。"""
    with history.history_engine().begin() as conn:
        history._source(conn, source_id)  # 不可读的来源：403
    configured = {_key(watch): watch for watch in watches() if watch.request.source_id == source_id}
    with (engine or publication.publication_engine()).begin() as conn:
        found = conn.execute(text("SELECT event_key, snapshot FROM publication_outbox WHERE event_key = ANY(:keys)"),
                             {"keys": list(configured)})
        saved = dict(found.all())
    rows = []
    for key, watch in configured.items():
        values = {name: saved.get(key, {}).get(name) for name in WATCH_FIELDS}
        rows.append({"watch_id": watch.id, "scope": watch.request.query, **values})

    def latest(name):
        return max((row[name] for row in rows if row[name]), key=parse_timestamp, default=None)

    failing = any(row["state"] in ("failed", "blocked") for row in rows)
    return {
        "source_id": source_id,
        "collection": {  # 这个来源的固定刷新记下的情况，不是整个来源的覆盖
            "state": "failed" if failing else "success" if latest("last_success_at") else "unknown",
            "last_recorded_success_at": latest("last_success_at"),
            "last_recorded_failure_at": latest("last_failure_at"),
            "scope": "recorded_crawl_task_not_whole_source",
            "coverage": "unknown",
        },
        "watches": rows,
    }
