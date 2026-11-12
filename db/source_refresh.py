"""服务器固定来源刷新及只读健康信息；复用原history RQ和发表控制行。"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import text

from biz_rules.history_models import (
    FetchRequest,
    HistoryError,
    Model,
    canonical,
    sha256,
)
from biz_rules.timestamps import parse_timestamp
from collection import rate, transport
from collection.catalog import CATALOG, WATCHABLE, spec
from collection.windows import cycle_request, first_unread
from db import history, publication


class SourceWatch(Model):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,60}$")
    request: FetchRequest
    cadence_seconds: int = Field(default=3600, gt=0, le=604800)  # the floor is the source's own, see query_budget
    lookback_days: int = Field(default=30, ge=1, le=3650)
    # realtime：按来源更新窗口增量读，窗口有持久检查点；backfill：不加窗口，按续页读完整个范围，
    # 读到底后按 cadence 重新对账。实时优先，回填在每轮实时之后各走一页（保底，不被饿死）。
    mode: Literal["realtime", "backfill"] = "realtime"

    @model_validator(mode="after")
    def query_budget(self):
        declared = CATALOG.get(self.request.source_id)
        if declared and self.cadence_seconds < declared.incremental.min_cadence_seconds:
            raise ValueError("刷新间隔短于该来源目录声明的下限")
        if self.mode == "backfill" and not (
            self.request.source_id in WATCHABLE and spec(self.request.source_id).backfill
        ):
            raise ValueError("该来源未声明可靠的全量续页，不能回填")
        if self.request.source_id in {"europe_pmc", "clinicaltrials"} and len(self.request.query) > 400:
            raise ValueError("论文/临床固定刷新查询须为更新日期窗预留长度")
        if self.request.source_id == "sec_companyfacts":
            count = len(self.request.source_record_ids)
            if (self.request.query != "*" or not count
                    or count > min(self.request.max_pages, self.request.max_records)):
                raise ValueError("SEC固定刷新须给明确概念清单，页数和记录预算须覆盖清单")
        return self


def _max_watches():
    try:
        value = int(os.environ.get("DATAFABRIC_HISTORY_MAX_WATCHES", "32"))
    except ValueError:
        value = -1
    if not 1 <= value <= 64:
        raise HistoryError("source_watch_configuration", "固定刷新数量上限须在1到64之间")
    return value


def watches():
    limit = _max_watches()
    try:
        data = json.loads(os.environ.get("DATAFABRIC_HISTORY_WATCHES", "[]"))
        if not isinstance(data, list) or len(data) > limit:
            raise ValueError()
        values = [SourceWatch.model_validate(value) for value in data]
        if len({value.id for value in values}) != len(values):
            raise ValueError()
        if any(
            value.request.source_id not in WATCHABLE
            or value.request.continuation is not None
            or value.request.updated_since is not None
            or value.request.updated_until is not None
            or value.request.known_stop  # 每轮由 collection.windows.cycle_request 按目录填，配置里不写
            for value in values
        ):
            raise ValueError()
        # 实时优先：同一轮里先走全部实时刷新，再给每个回填走一页
        return sorted(values, key=lambda value: value.mode != "realtime")
    except (ValueError, ValidationError):
        raise HistoryError(
            "source_watch_configuration",
            "固定刷新配置无效；只允许来源目录中已接通且有增量键的来源",
        ) from None


def _key(watch):
    data = watch.model_dump(mode="json")
    if data.get("mode") == "realtime":
        data.pop("mode")  # 改造前已在运行的刷新保持原键，状态和窗口不重置
    data["request"].pop("known_stop")  # 同理：配置里本来就不写（watches 拒绝非 0），新加的字段不能改动已有的键
    return "source-watch:" + sha256(canonical(data))


def _save(engine, key, state):
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE publication_outbox SET snapshot=CAST(:state AS jsonb) "
                "WHERE event_key=:key AND snapshot->>'lease_token'=:token"
            ),
            {"key": key, "state": canonical(state), "token": state["lease_token"]},
        )
        if result.rowcount != 1:
            raise HistoryError(
                "source_watch_lease_lost", "刷新处理资格已改变，当前持有者继续", 409
            )


def _hold_at_ceiling(watch, state, coverage, now):
    """The read stopped at an outbound ceiling (collection.rate). That is neither a failure nor a loss: the reason is
    the run's stop_reason, kept in last_coverage. Pages already read stay read. The cycle ends without success and
    without moving the checkpoint, and the next cycle resumes at the cursor, or, when no page was read, plans its
    window again from the same checkpoint."""
    if coverage.get("next_continuation"):
        state["request"]["continuation"] = coverage["next_continuation"]
    elif not state["request"].get("continuation"):
        state.pop("request")
    state["state"] = "idle"
    state["next_check_at"] = (now + timedelta(seconds=watch.cadence_seconds)).isoformat()


def _page_finished(watch, state, coverage, now):
    """Record a read that ended without error: ask for the next page, or end the cycle.

    A read that stopped at an outbound ceiling ends the cycle with that reason (_hold_at_ceiling). Any other read that
    stopped short without a cursor is an error, except a windowed one (GDELT): its receipts show which intervals may
    be cut short and which windows were not reached. The cycle ends with those gaps left in the receipts, and the
    checkpoint moves to the first window not reached so that the next cycle reads from there."""
    state["last_page_at"] = now.isoformat()
    state["last_coverage"] = coverage
    state["cycle_pages"] = int(state.get("cycle_pages", 0)) + 1
    if coverage.get("stop_reason") in rate.CEILING_STOPS and "window_coverage" not in coverage:
        return _hold_at_ceiling(watch, state, coverage, now)
    if coverage.get("next_continuation"):
        state["request"]["continuation"] = coverage["next_continuation"]
        state["state"] = "paging"
        state["next_check_at"] = now.isoformat()
        return
    checkpoint, finished = state.get("window_ended_at"), True
    # 增量读取读到了已存且没变的原件（reached_known）：前面都是读过的，这一周期到此为止，不是“截断”
    if coverage.get("upstream_pagination_exhausted") is not True and coverage.get("stop_reason") != transport.KNOWN:
        if "window_coverage" not in coverage:
            # 截断且无可靠续页位置不能称完成，更不能跳过未知页。
            raise HistoryError(
                "source_watch_no_cursor",
                "来源截断但无可靠续页位置；保留本页等待处理",
                503,
            )
        unread = first_unread(coverage)
        checkpoint, finished = unread or checkpoint, unread is None
    state["state"] = "idle"
    if finished:
        state["last_success_at"] = now.isoformat()
        if watch.mode == "backfill":
            state["backfill_completed_at"] = now.isoformat()
    if checkpoint:
        # 窗口已确认读到底，或读到第一个没读到的窗口为止：下一周期从这里（减重叠）续读
        state["window_checkpoint"] = checkpoint
    state["next_check_at"] = (now + timedelta(seconds=watch.cadence_seconds)).isoformat()
    state.pop("request", None)


def _plan_cycle(watch, state, now):
    """新的一个周期（state 里没有进行中的请求）：定这一周期读什么。失败后的重试不走这里，原请求还留在 state 里。"""
    request, start, end = cycle_request(watch, state, now)
    state.update(
        mode=watch.mode,
        fixed_scope_query=request["query"],
        window_started_at=start.isoformat() if start else None,
        window_ended_at=end.isoformat() if end else None,
        request=request,
        cycle_started_at=now.isoformat(),
        cycle_pages=0,
    )


def _read_all_next_time(state):
    """这一页的读取失败了，或有条目读不到（运行没读干净）：下一次重试不许“遇到已存的就停”。前面几条是已存的，
    停在它们上面，就把后面没存的跳过去了；整份读到底，没存的才会再碰到、补上。"""
    state["request"]["known_stop"] = 0


def refresh_once(engine=None):
    """仅协调已配置的固定抓取；没有配置时不联网、不启动采集。"""
    configured = watches()
    if not configured:
        return []
    engine = engine or publication.publication_engine()
    results = []
    for watch in configured:
        key = _key(watch)
        # 短锁只保留意图和lease，历史库/Redis调用全部在锁外。
        with engine.begin() as conn:
            now = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            conn.execute(
                text("""
              INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
              VALUES (:key,'control',:key,'{}'::jsonb) ON CONFLICT(event_key) DO NOTHING
            """),
                {"key": key},
            )
            row = conn.execute(
                text(
                    "SELECT snapshot FROM publication_outbox WHERE event_key=:key FOR UPDATE"
                ),
                {"key": key},
            ).scalar_one()
            state = dict(row)
            if (
                state.get("lease_until")
                and parse_timestamp(state["lease_until"]) > now
            ):
                continue
            if (
                state.get("state") in {"failed", "blocked", "idle"}
                and state.get("next_check_at")
                and parse_timestamp(state["next_check_at"]) > now
            ):
                continue
            state.update(
                watch_id=watch.id,
                source_id=watch.request.source_id,
                cadence_seconds=watch.cadence_seconds,
                scope=watch.request.query,
                lease_until=(now + timedelta(seconds=180)).isoformat(),
                lease_token=uuid4().hex,
            )
            if "request" not in state:
                _plan_cycle(watch, state, now)
            if not state.get("active_run_id") and not state.get("request_key"):
                # 先持久意图：create_fetch提交后丢回复，下轮以同key找回原run。
                state["generation"] = int(state.get("generation", 0)) + 1
                state["request_key"] = key + ":" + str(state["generation"])
                state["last_check_at"] = now.isoformat()
                state["state"] = "starting"
            conn.execute(
                text(
                    "UPDATE publication_outbox SET snapshot=CAST(:state AS jsonb) WHERE event_key=:key"
                ),
                {"key": key, "state": canonical(state)},
            )
        try:
            with history.history_engine().begin() as permit:
                history._source(permit, watch.request.source_id)
            if publication.disk_status()["backfill_paused"]:
                raise HistoryError(
                    "source_watch_disk_low",
                    "磁盘低水位暂停新增来源读取；续页意图保留",
                    503,
                )
            from flows.history_fetch import enqueue_fetch, retry_inactive

            if state.get("active_run_id"):
                run = history.read_run(state["active_run_id"])
            else:
                run, _ = history.create_fetch(
                    FetchRequest.model_validate(state["request"]), state["request_key"]
                )
                state["active_run_id"] = run.id
                _save(engine, key, state)
            if run.state in {"queued", "enqueue_failed", "running"}:
                if run.state == "queued":
                    enqueue_fetch(
                        run
                    )  # 固定job_id核对后入队；崩溃遗留的死job随后显式恢复。
                try:
                    retry_inactive(run.id, run.attempt)
                except HistoryError as exc:
                    if exc.code != "job_may_be_active":
                        raise
                state["state"] = "running"
            elif run.state in {"completed", "partial"} and not run.error:
                _page_finished(watch, state, run.coverage, now)
                state.pop("active_run_id", None)
                state.pop("request_key", None)
                state["error"] = None
            else:
                raise HistoryError(
                    "source_watch_failed", "固定来源读取失败；本页保留，下周期重试", 503
                )
        except HistoryError as exc:
            state["state"] = "blocked" if exc.status == 403 else "failed"
            state["error"] = {"code": exc.code, "message": exc.message}
            state["last_failure_at"] = now.isoformat()
            state["next_check_at"] = (
                now + timedelta(seconds=watch.cadence_seconds)
            ).isoformat()
            # 已完成失败页用新run重试相同范围/续页；未知create/入队结果保留固定key。
            if state.get("active_run_id"):
                try:
                    saved = history.read_run(state["active_run_id"])
                    if saved.state in {"failed", "partial"}:
                        state.pop("active_run_id", None)
                        state.pop("request_key", None)
                        _read_all_next_time(state)
                except HistoryError:
                    pass
        finally:
            state["lease_until"] = None
            _save(engine, key, state)
        results.append({"watch_id": watch.id, "state": state["state"]})
    return results


WATCH_FIELDS = (  # 健康输出里每个固定刷新给出的状态字段
    "watch_id", "mode", "state", "scope", "fixed_scope_query", "window_started_at", "window_ended_at",
    "window_checkpoint", "backfill_completed_at", "cadence_seconds", "last_check_at", "last_success_at",
    "last_failure_at", "last_page_at", "active_run_id", "next_check_at", "cycle_started_at", "cycle_pages",
    "last_coverage", "error",
)
STUCK_ROUNDS = 3  # 同一条连续这么多轮都读不到，健康输出里单列出来


def stuck_items(source_id):
    """连续 STUCK_ROUNDS 轮都读不到的条目 [{"url", "reason", "rounds"}]。数据就是各次读取留在 coverage.unread_items 里的
    （网址 + 原因），不另建表。一轮 = 同一个读取范围（query）的一次读取；整次失败的一轮没有 unread_items，连续数就断了。"""
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='3000ms'"))
        rows = conn.execute(
            text("""
          SELECT request_payload->>'query', coverage->'unread_items' FROM history_runs
          WHERE source_id=:source AND kind='fetch' AND NOT is_demo AND state IN ('completed','partial','failed')
          ORDER BY created_at DESC LIMIT 30
        """),
            {"source": source_id},
        ).all()
    rounds = {}  # 读取范围 → 它最近几轮各自读不到的 {网址: 原因}，新的在前
    for query, unread in rows:
        if len(rounds.setdefault(query, [])) < STUCK_ROUNDS:
            rounds[query].append({item["url"]: item["reason"] for item in unread or []})
    return [
        {"url": url, "reason": reason, "rounds": STUCK_ROUNDS}
        for recent in rounds.values()
        if len(recent) == STUCK_ROUNDS
        for url, reason in recent[0].items()
        if all(url in earlier for earlier in recent[1:])
    ]


def source_health(source_id, engine=None):
    with history.history_engine().begin() as permit:
        history._source(permit, source_id)
    engine = engine or publication.publication_engine()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='3000ms'"))
        observed = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        # 仅返回明确受许可的该来源；原配置/异常正文可能含密钥，不回显。
        logs = list(
            conn.execute(
                text("""
          SELECT id,status,started_at,finished_at,total_count,passed_count,failed_count,task_type
          FROM crawl_log WHERE site_key=:source AND NOT is_deleted
          ORDER BY started_at DESC,id DESC LIMIT 20
        """),
                {"source": source_id},
            ).mappings()
        )
        success_row = (
            conn.execute(
                text("""
          SELECT finished_at FROM crawl_log WHERE site_key=:source AND NOT is_deleted
            AND status='success' AND finished_at IS NOT NULL AND failed_count=0
          ORDER BY finished_at DESC,id DESC LIMIT 1
        """),
                {"source": source_id},
            )
            .mappings()
            .first()
        )
        failure_row = (
            conn.execute(
                text("""
          SELECT started_at,finished_at FROM crawl_log WHERE site_key=:source AND NOT is_deleted
            AND (status='failed' OR (finished_at IS NOT NULL AND failed_count>0))
          ORDER BY COALESCE(finished_at,started_at) DESC,id DESC LIMIT 1
        """),
                {"source": source_id},
            )
            .mappings()
            .first()
        )
        controls = list(
            conn.execute(
                text(
                    "SELECT event_key,kind,snapshot FROM publication_outbox WHERE kind IN ('control','backfill')"
                )
            ).mappings()
        )
        pending_counts = None
        if source_id in publication.source_sites():
            pending_counts = {
                name: 0 for name in ("pending", "processing", "blocked", "failed")
            }
            pending_counts.update(
                {
                    row["state"]: row["n"]
                    for row in conn.execute(
                        text("""
              SELECT state,count(*) AS n FROM publication_outbox
              WHERE kind='document' AND state IN ('pending','processing','blocked','failed')
                AND snapshot->'document'->>'source_site'=:source GROUP BY state
            """),
                        {"source": source_id},
                    ).mappings()
                }
            )
    latest = logs[0] if logs else None
    success, failed = success_row, failure_row

    def stamp(value):
        return value.isoformat() if value else None

    collection = {
        "state": latest["status"] if latest else "unknown",
        "last_check_at": stamp(latest["started_at"]) if latest else None,
        "last_recorded_success_at": stamp(success["finished_at"]) if success else None,
        "last_recorded_failure_at": stamp(failed["finished_at"] or failed["started_at"])
        if failed
        else None,
        "scope": "recorded_crawl_task_not_whole_source",
        "coverage": "unknown",
        "latest_log_id": latest["id"] if latest else None,
        "counts": {
            name: latest[name] if latest and latest["finished_at"] else None
            for name in ("total_count", "passed_count", "failed_count")
        },
    }
    seeds = []
    watch_states = []
    runtime = None
    current_watches = {_key(value) for value in watches()}
    for row in controls:
        payload = row["snapshot"]
        if row["kind"] == "backfill" and source_id in payload.get("source_sites", []):
            progress = payload.get("sites", {}).get(source_id)
            if progress is None:
                progress = {
                    "last_id": payload["last_id"],
                    "max_id": payload["max_id"],
                    "complete": payload["last_id"] >= payload["max_id"],
                }
            seeds.append(
                {
                    **progress,
                    "scope": "initial_authorized_inventory_not_complete_source",
                }
            )
        if payload.get("source_id") == source_id and payload.get("watch_id"):  # noqa: SIM102
            # 配置已删除的watch不能继续被误认为在刷新。
            if row["event_key"] in current_watches:
                watch_states.append({key: payload.get(key) for key in WATCH_FIELDS})
        if "last_heartbeat" in payload:
            runtime = {
                "last_heartbeat": payload["last_heartbeat"],
                "state": payload.get("publisher_state", "unknown"),
            }
    if runtime is not None:
        runtime["pending_counts"] = pending_counts
        runtime["counts_scope"] = (
            "documents_store_bridge" if pending_counts is not None else "not_applicable"
        )
    if runtime and runtime.get("last_heartbeat"):  # noqa: SIM102
        if (
            observed - parse_timestamp(runtime["last_heartbeat"])
        ).total_seconds() > 120:
            runtime["state"] = "stale"
    for value in watch_states:
        last = value.get("last_check_at")
        value["stale"] = (
            last is None
            or (observed - parse_timestamp(last)).total_seconds()
            > value["cadence_seconds"] + 180
        )
    return {
        "schema_version": "datafabric.source.health.v1",
        "source_id": source_id,
        "observed_at": observed.isoformat(),
        "collection": collection,
        "publication": runtime or {"last_heartbeat": None, "state": "unknown"},
        "seed": seeds,
        "watches": watch_states,
        "stuck_items": stuck_items(source_id),  # 连续 3 轮读不到的条目；空 = 没有
        "limitations": [
            "CrawlLog仅说明所记录任务；缺日志不补零，成功不证明全站完整",
            "发表器心跳/采用日志不是原站最后检查；库存EOF仅固定旧库存范围",
            "WHO是选择性公告，Europe PMC是显式查询；均不证明领域无事件",
        ],
    }
