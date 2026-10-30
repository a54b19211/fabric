"""服务器固定来源刷新及只读健康信息；复用原history RQ和发表控制行。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
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
from db import history, publication


class SourceWatch(Model):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,60}$")
    request: FetchRequest
    cadence_seconds: int = Field(default=3600, ge=3600, le=604800)
    lookback_days: int = Field(default=30, ge=1, le=3650)

    @model_validator(mode="after")
    def query_budget(self):
        if self.request.source_id in {"europe_pmc", "clinicaltrials"} and len(self.request.query) > 400:
            raise ValueError("论文/临床固定刷新查询须为更新日期窗预留长度")
        if self.request.source_id == "sec_companyfacts":
            count = len(self.request.source_record_ids)
            if (self.request.query != "*" or not count
                    or count > min(self.request.max_pages, self.request.max_records)):
                raise ValueError("SEC固定刷新须给明确概念清单，页数和记录预算须覆盖清单")
        return self


def watches():
    try:
        data = json.loads(os.environ.get("DATAFABRIC_HISTORY_WATCHES", "[]"))
        if not isinstance(data, list) or len(data) > 8:
            raise ValueError()
        values = [SourceWatch.model_validate(value) for value in data]
        if len({value.id for value in values}) != len(values):
            raise ValueError()
        if any(
            value.request.source_id not in {"who_don", "europe_pmc", "clinicaltrials", "sec_companyfacts"}
            or value.request.continuation is not None
            or value.request.updated_since is not None
            or value.request.updated_until is not None
            for value in values
        ):
            raise ValueError()
        return values
    except (ValueError, ValidationError):
        raise HistoryError(
            "source_watch_configuration",
            "固定刷新配置无效；仅允许WHO、Europe PMC、临床登记和SEC明确范围",
        ) from None


def _key(watch):
    return "source-watch:" + sha256(canonical(watch.model_dump(mode="json")))


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
                and datetime.fromisoformat(state["lease_until"]) > now
            ):
                continue
            if (
                state.get("state") in {"failed", "blocked", "idle"}
                and state.get("next_check_at")
                and datetime.fromisoformat(state["next_check_at"]) > now
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
                request = watch.request.model_dump(mode="json")
                if watch.request.source_id == "who_don":
                    request.update(
                        updated_since=(
                            now - timedelta(days=watch.lookback_days)
                        ).isoformat(),
                        updated_until=now.isoformat(),
                    )
                if watch.request.source_id == "europe_pmc":
                    start = (
                        (
                            now.astimezone(timezone.utc)
                            - timedelta(days=watch.lookback_days)
                        )
                        .date()
                        .isoformat()
                    )
                    end = now.astimezone(timezone.utc).date().isoformat()
                    request["query"] = (
                        "("
                        + request["query"]
                        + ") AND UPDATE_DATE:["
                        + start
                        + " TO "
                        + end
                        + "]"
                    )
                if watch.request.source_id == "clinicaltrials" and not request["source_record_ids"]:
                    start = (now - timedelta(days=watch.lookback_days)).date().isoformat()
                    end = now.date().isoformat()
                    date_filter = "AREA[LastUpdatePostDate]RANGE[" + start + "," + end + "]"
                    request["query"] = (
                        date_filter if request["query"] == "*"
                        else "(" + request["query"] + ") AND " + date_filter
                    )
                state["fixed_scope_query"] = request["query"]
                state["window_started_at"] = (
                    now - timedelta(days=watch.lookback_days)
                ).isoformat()
                state["window_ended_at"] = now.isoformat()
                state["request"] = request
                state["cycle_started_at"] = now.isoformat()
                state["cycle_pages"] = 0
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
                state["last_page_at"] = now.isoformat()
                state["last_coverage"] = run.coverage
                state["cycle_pages"] = int(state.get("cycle_pages", 0)) + 1
                next_page = run.coverage.get("next_continuation")
                if next_page:
                    state["request"]["continuation"] = next_page
                    state["state"] = "paging"
                    state["next_check_at"] = now.isoformat()
                elif run.coverage.get("upstream_pagination_exhausted") is True:
                    state["state"] = "idle"
                    state["last_success_at"] = now.isoformat()
                    state["next_check_at"] = (
                        now + timedelta(seconds=watch.cadence_seconds)
                    ).isoformat()
                    state.pop("request", None)
                else:
                    # 截断且无可靠续页位置不能称完成，更不能跳过未知页。
                    raise HistoryError(
                        "source_watch_no_cursor",
                        "来源截断但无可靠续页位置；保留本页等待处理",
                        503,
                    )
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
                except HistoryError:
                    pass
        finally:
            state["lease_until"] = None
            _save(engine, key, state)
        results.append({"watch_id": watch.id, "state": state["state"]})
    return results


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
                watch_states.append(
                    {
                        key: payload.get(key)
                        for key in (
                            "watch_id",
                            "state",
                            "scope",
                            "fixed_scope_query",
                            "window_started_at",
                            "window_ended_at",
                            "cadence_seconds",
                            "last_check_at",
                            "last_success_at",
                            "last_failure_at",
                            "last_page_at",
                            "active_run_id",
                            "next_check_at",
                            "cycle_started_at",
                            "cycle_pages",
                            "last_coverage",
                            "error",
                        )
                    }
                )
        if "last_heartbeat" in payload:
            runtime = {
                "last_heartbeat": payload["last_heartbeat"],
                "state": payload.get("publisher_state", "unknown"),
            }
    if runtime is not None:
        runtime["pending_counts"] = pending_counts
        runtime["counts_scope"] = (
            "legacy_source_bridge" if pending_counts is not None else "not_applicable"
        )
    if runtime and runtime.get("last_heartbeat"):  # noqa: SIM102
        if (
            observed - datetime.fromisoformat(runtime["last_heartbeat"])
        ).total_seconds() > 120:
            runtime["state"] = "stale"
    for value in watch_states:
        last = value.get("last_check_at")
        value["stale"] = (
            last is None
            or (observed - datetime.fromisoformat(last)).total_seconds()
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
        "limitations": [
            "CrawlLog仅说明所记录任务；缺日志不补零，成功不证明全站完整",
            "发表器心跳/采用日志不是原站最后检查；库存EOF仅固定旧库存范围",
            "WHO是选择性公告，Europe PMC是显式查询；均不证明领域无事件",
        ],
    }
