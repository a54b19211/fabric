"""服务器固定来源刷新及只读健康信息；复用原history RQ和发表控制行。"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import text

from fetch import transport
from fetch.catalog import CATALOG, WATCHABLE, spec
from fetch.windows import KNOWN_STOP, SEGMENTS_PER_CYCLE, cycle_request, first_unread
from graph.publish import publication
from store import history
from store.history_models import (
    FetchRequest,
    HistoryError,
    Model,
    canonical,
    sha256,
)
from store.timestamps import parse_timestamp


class SourceWatch(Model):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,60}$")
    request: FetchRequest
    cadence_seconds: int = Field(default=3600, gt=0, le=604800)  # 下限由来源目录定，见 query_budget
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
            or value.request.known_stop  # 每轮由 fetch.windows.cycle_request 按目录填，配置里不写
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


def _stalled(state, coverage):
    """续读位置和请求里的一样，而且这一段一个版本也没读到：原地没动（比如重放就用完了截止时间）。"""
    before = state["request"].get("continuation") or {}
    after = coverage["next_continuation"]
    return coverage.get("adopted_count") == 0 and all(after.get(key) == before.get(key) for key in ("cursor", "read_count"))


def _page_finished(watch, state, coverage, now):
    """记下一次没有出错的读取：要下一段就接着读，或者结束这一周期。

    没有续读位置的提前停止都是错误，除了按窗口读的
    （GDELT）：它的回执写明哪些区间可能被截断、哪些窗口没读到，周期带着这些缺口结束，检查点移到第一个没读到的窗口。
    “遇到已存的就停”的来源一个周期最多读 SEGMENTS_PER_CYCLE 段就收工记成功：第一次读一个很深的来源（空库）不会
    一个周期接一个周期地读下去，更老的交给回填（ops/backfill_source.py）。分组读取的来源（港交所、NCCN）不按段数收工：
    一个周期读完全部的组，每组读多少由 fetch.windows.GROUP_LIMIT 管。"""
    state["last_page_at"] = now.isoformat()
    state["last_coverage"] = coverage
    state["cycle_pages"] = int(state.get("cycle_pages", 0)) + 1
    if _unread_not_stuck(watch.request.source_id, coverage):
        state["unclean"] = True  # 这一周期有条目读不到：周期收尾时记下，下一周期整份重读（read_all）
    capped = _cycle_is_full(watch, state)
    if coverage.get("next_continuation"):
        if _stalled(state, coverage):
            raise HistoryError("source_watch_no_progress", "续读位置没有前进，这一段一个版本也没读到；下个间隔再试", 503)
        if not capped:
            state["request"]["continuation"] = coverage["next_continuation"]
            _known_stop_again(watch, state)
            state["state"] = "paging"
            state["next_check_at"] = now.isoformat()
            return
    checkpoint, finished = state.get("window_ended_at"), True
    # 增量读取读到了已存且没变的原件（reached_known）：前面都是读过的，这一周期到此为止，不是“截断”
    ended = coverage.get("upstream_pagination_exhausted") is True or coverage.get("stop_reason") in (transport.KNOWN, "items_unread")
    if not (capped or ended):
        if "window_coverage" not in coverage:
            # 截断且无可靠续页位置不能称完成，更不能跳过未知页。
            raise HistoryError(
                "source_watch_no_cursor",
                "来源截断但无可靠续页位置；按读取失败记，下个间隔重读，连续 3 次放弃这一周期",
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
    state["read_all"] = state.pop("unclean", False)
    state.pop("request", None)


def _cycle_is_full(watch, state):
    """“遇到已存的就停”的实时刷新这一周期已经读够了 SEGMENTS_PER_CYCLE 段。分组读取的来源不按段数：一个周期读完全部的组。"""
    incremental = spec(watch.request.source_id).incremental
    return (watch.mode == "realtime" and incremental.kind == "stop_at_known" and not incremental.by_group
            and state["cycle_pages"] >= SEGMENTS_PER_CYCLE)


def _known_stop_again(watch, state):
    """分组读取的来源：失败后重读（_read_all_next_time）只管那一段，下一段又按组停在已存的。"""
    if spec(watch.request.source_id).incremental.by_group and watch.mode == "realtime" and not watch.request.source_record_ids:
        state["request"]["known_stop"] = KNOWN_STOP


def _unread_not_stuck(source_id, coverage):
    """这一段读不到的条目里，还没进 stuck_items 的。连续几轮都读不到的（死链）不再为它整份重读，不然每个周期都重读。"""
    urls = [item["url"] for item in coverage.get("unread_items") or []]
    if not urls:
        return []
    stuck = {item["url"] for item in stuck_items(source_id)}
    return [url for url in urls if url not in stuck]


def _plan_cycle(watch, state, now):
    """新的一个周期（state 里没有进行中的请求）：定这一周期读什么。失败后的重试不走这里，原请求还留在 state 里。"""
    request, start, end = cycle_request(watch, state, now)  # 上一周期有条目读不到（read_all）：这一周期不用 known_stop
    state.pop("read_all", None)
    state.update(
        mode=watch.mode,
        fixed_scope_query=request["query"],
        window_started_at=start.isoformat() if start else None,
        window_ended_at=end.isoformat() if end else None,
        request=request,
        cycle_started_at=now.isoformat(),
        cycle_pages=0,
    )


GIVE_UP_AFTER = 3  # 同一周期连续失败这么多次，就放弃这一周期（只有 realtime；回填保留续读位置，按间隔重试）
READ_FAILURES = ("source_watch_failed", "source_watch_no_progress", "source_watch_no_cursor")  # 计数的只有这次读取本身失败、原地不动、读完了没给续读位置；磁盘低水位、许可撤回等不计


def _read_all_next_time(state):
    """这一页的读取失败了：下一次重试不许“遇到已存的就停”。前面几条是已存的，停在它们上面，就把后面没存的跳过去了；
    往后读，没存的才会再碰到、补上。（读到了但有条目读不到的，不在这里：周期收尾时记 read_all，下一周期整份重读；
    分组读取的来源不整份重读，读不到的组下个周期自然再读。）"""
    state["request"]["known_stop"] = 0


def _claim(engine, key, watch):
    """短锁只保留意图和 lease，历史库/Redis 调用全部在锁外。到点要读就返回 (状态, 现在)，还没到点或别人持有 lease 返回 None。"""
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
            text("SELECT snapshot FROM publication_outbox WHERE event_key=:key FOR UPDATE"),
            {"key": key},
        ).scalar_one()
        state = dict(row)
        if state.get("lease_until") and parse_timestamp(state["lease_until"]) > now:
            return None
        if (
            state.get("state") in {"failed", "blocked", "idle"}
            and state.get("next_check_at")
            and parse_timestamp(state["next_check_at"]) > now
        ):
            return None
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
            text("UPDATE publication_outbox SET snapshot=CAST(:state AS jsonb) WHERE event_key=:key"),
            {"key": key, "state": canonical(state)},
        )
    return state, now


def _advance(engine, key, watch, state, now):
    """这一次要做的一步：建 run 或找回 run，入队，读完的 run 交给 _page_finished。出错抛 HistoryError（由 _failed 记下）。"""
    with history.history_engine().begin() as permit:
        history._source(permit, watch.request.source_id)
    if publication.disk_status()["backfill_paused"]:
        raise HistoryError("source_watch_disk_low", "磁盘低水位暂停新增来源读取；续页意图保留", 503)
    from fetch.fetch_job import JOB_TIMEOUT, enqueue_fetch

    if state.get("active_run_id"):
        run = history.read_run(state["active_run_id"])
    else:
        run, _ = history.create_fetch(FetchRequest.model_validate(state["request"]), state["request_key"])
        state["active_run_id"] = run.id
        _save(engine, key, state)
    if run.state in {"queued", "enqueue_failed", "running"}:
        if (now - run.created_at).total_seconds() > 3 * JOB_TIMEOUT:
            # 采集工人中途死了，任务停在 queued/running：记失败，下次换一个新任务重读这一段
            history.fail_job(run.id, run.attempt, "run_stalled", "采集任务长时间没有结束")
            raise HistoryError("source_watch_failed", "采集任务长时间没有结束；本页保留，下周期重试", 503)
        if run.state == "queued":
            enqueue_fetch(run)  # 已经在队列里的不会再放一次
        state["state"] = "running"
    elif run.state in {"completed", "partial"} and (not run.error or run.coverage.get("stop_reason") == "items_unread"):
        _page_finished(watch, state, run.coverage, now)  # items_unread：读到底了，只是有几条读不到，不是失败
        state["fail_streak"] = 0  # 正常读完一页（没抛错）才算连续失败断了
        state.pop("active_run_id", None)
        state.pop("request_key", None)
        state["error"] = None
    else:
        raise HistoryError("source_watch_failed", "固定来源读取失败；本页保留，下周期重试", 503)


def _failed(watch, state, exc, now):
    """这一步出错：记下原因，按间隔重试；已读完的失败页换新 run 重试相同范围/续页。读取本身连续失败多次，realtime 放弃这一周期；
    回填不放弃（丢了续读位置就要从头读起），保留位置按间隔重试。"""
    state["state"] = "blocked" if exc.status == 403 else "failed"
    state["error"] = {"code": exc.code, "message": exc.message}
    state["last_failure_at"] = now.isoformat()
    state["next_check_at"] = (now + timedelta(seconds=watch.cadence_seconds)).isoformat()
    # 未知 create/入队结果保留固定 key。只有运行本身失败（failed，或带着错误的 partial）才换新 run；正常的 partial
    # （只是预算用完）不是失败。续读位置没前进、或读完了却没给续读位置的那一段，run 本身也没错，同样要换新 run 重读
    # （否则每个间隔都取回同一个结果，永远卡在这里）。
    if state.get("active_run_id"):
        try:
            saved = history.read_run(state["active_run_id"])
            if saved.state == "failed" or saved.error or exc.code in ("source_watch_no_progress", "source_watch_no_cursor"):
                state.pop("active_run_id", None)
                state.pop("request_key", None)
                _read_all_next_time(state)
        except HistoryError:
            pass
    if exc.code not in READ_FAILURES:
        return
    state["fail_streak"] = int(state.get("fail_streak", 0)) + 1
    if watch.mode == "realtime" and state["fail_streak"] >= GIVE_UP_AFTER and "active_run_id" not in state:
        # 同一周期连续失败这么多次：放弃这一周期，下个间隔从头（窗口来源从检查点）重新规划，不永远卡在同一个位置
        state.pop("request", None)
        state["fail_streak"] = 0
        state["read_all"] = True  # 这一周期没读完，下一周期别在“遇到已存的”就停
        state.pop("unclean", None)


def refresh_once(engine=None):
    """仅协调已配置的固定抓取；没有配置时不联网、不启动采集。"""
    configured = watches()
    if not configured:
        return []
    engine = engine or publication.publication_engine()
    results = []
    for watch in configured:
        key = _key(watch)
        claimed = _claim(engine, key, watch)
        if claimed is None:
            continue
        state, now = claimed
        try:
            _advance(engine, key, watch, state, now)
        except HistoryError as exc:
            _failed(watch, state, exc, now)
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
    （网址 + 原因），不另建表。一轮 = 同一个读取范围（query）从第一段（没有续读位置的那一段）起的一次整读，
    它的各段合在一起；整次失败的一轮没有 unread_items，连续数就断了。"""
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='3000ms'"))
        rows = conn.execute(
            text("""
          SELECT request_payload->>'query', request_payload->'continuation' IS NULL, coverage->'unread_items'
          FROM history_runs
          WHERE source_id=:source AND kind='fetch' AND NOT is_demo AND state IN ('completed','partial','failed')
          ORDER BY created_at DESC LIMIT 60
        """),
            {"source": source_id},
        ).all()
    rounds, reading = {}, {}  # 读取范围 → 它最近几轮各自读不到的 {网址: 原因}（新的在前）/ 正在合并的这一轮
    for query, first_segment, unread in rows:  # 新的在前：碰到一轮的第一段，这一轮就齐了
        this_round = reading.setdefault(query, {})
        for item in unread or []:
            this_round.setdefault(item["url"], item["reason"])
        if first_segment:
            if len(rounds.setdefault(query, [])) < STUCK_ROUNDS:
                rounds[query].append(reading.pop(query))
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
