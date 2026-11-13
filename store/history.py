"""History 库：存版本、记采集任务、按模式和截止时间读版本。

存（finish_fetch 抓取 / import_archive 导入 → _publish，一个事务）：
  每条原件算一个内容指纹 fingerprint(source_id, raw)，和这条记录当前版本（replaced_by 为空的那个）的指纹比：
  相同 → 不存，记"没变"；不同 → 插一个新版本（来源的 published_seq + 1），旧版本的 replaced_by 指向新版本。
  原件只追加：已存的版本只会被补上 fingerprint、replaced_by 两列，别的不改不删。A→B→A 是第三个版本。
读（list_records、read_version、list_changes、visible_versions）：
  current / operational_replay 模式看 first_seen_at <= cutoff；public_history_reconstruction 看 proof_available_at
  （档案导入时声明的公开可见时间）。一条记录在截止时间下可见的版本按 _LATEST 排第一的那个。

表：history_sources（来源，read_enabled 关掉就不可读）、history_runs（采集/导入任务）、history_versions（版本）、
history_version_rank（排序用的派生键）、history_attachment_links（附件属于哪个父页面版本）。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from functools import lru_cache

from sqlalchemy import create_engine, text

from clean import registry_changes
from fetch.catalog import GRAPH_SOURCES
from fetch.sources import project_record
from store.history_models import (
    MAX_RESPONSE_BYTES,
    ArchiveImport,
    AvailabilityProof,
    FetchRequest,
    HistoryError,
    HistoryRecord,
    RecordCollection,
    RunView,
    canonical,
    sha256,
    utc,
)
from store.timestamps import parse_timestamp


@lru_cache(maxsize=2)
def _engine(url):
    return create_engine(url, pool_size=3, max_overflow=2, pool_pre_ping=True, connect_args={"connect_timeout": 5})


def history_engine():
    # 不回退到别的数据库地址：没配就报错，免得工作树误连现役库
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL", "")
    if not url:
        raise HistoryError("history_not_configured", "历史资料数据库尚未配置", 503)
    return _engine(url)


def _now(conn):
    return conn.execute(text("SELECT clock_timestamp()")).scalar_one()


def _source(conn, source_id, *, lock=False):
    """来源那一行；不存在或 read_enabled 关了就不可读（403）。lock：存版本时锁住来源行，同来源的存版本排队
    （FOR NO KEY UPDATE：只改 published_seq，不挡插版本时外键要的锁）。"""
    row = conn.execute(
        text("SELECT * FROM history_sources WHERE source_id=:id" + (" FOR NO KEY UPDATE" if lock else "")),
        {"id": source_id},
    ).mappings().first()
    if row is None or row["read_enabled"] is not True:
        raise HistoryError("source_not_available", "该来源当前不可读取", 403)
    return row


def sources_receipt():
    """GET /api/history/sources：可读的来源和已退役的来源，各带名称和雷达读的几项能力（store/source_listing.py）。"""
    from store import source_listing

    with history_engine().begin() as conn:
        rows = list(conn.execute(text(
            "SELECT source_id, read_enabled, catalog FROM history_sources ORDER BY source_id")).mappings())
    return source_listing.listing(rows)


def list_sources():
    return sources_receipt()["sources"]


# ---- 内容指纹 -------------------------------------------------------------------------------------------

# 来源每次响应都会改写、但不说明记录变了的字段：ClinicalTrials 给每条登记盖上数据快照日期
VOLATILE_FIELDS = {"clinicaltrials": [("derivedSection", "miscInfoModule", "versionHolder")]}
# 网页抓取（role=web_capture）保存了当时的 HTML，但 HTML 每次都不一样（随机 id、每次新的下载路径）：
# 比较的是解析出的标题、正文和附件清单，HTML 里解析没读到的变动不算新版本
WEB_CAPTURE_HTML = ("html", "html_sha256", "html_store_path")


def normalized_raw(source_id: str, raw: str) -> str:
    """比较两份原件用的文本：JSON 原件去掉上面两类字段后按键排序重新写出（Europe PMC 同一条记录每次返回的键顺序
    不同，曾让 8444 条记录出了 23927 个版本）；不是 JSON 的原件（PubMed XML）就是原文。"""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(data, dict):
        if data.get("role") == "web_capture":
            for key in WEB_CAPTURE_HTML:
                data.pop(key, None)
        for *path, last in VOLATILE_FIELDS.get(source_id, ()):
            node = data
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, dict):
                node.pop(last, None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(source_id: str, raw: str) -> str:
    """原件的内容指纹：同一条记录两次读到的原件指纹相同，就是没变。"""
    return sha256(normalized_raw(source_id, raw))


# ---- 存版本 ---------------------------------------------------------------------------------------------


def _role(projected) -> str:
    """版本的身份：来源自己的记录（source_record），或 legacy 迁入的（migrated_record）。两类各比各的当前版本，
    迁入的旧内容不会顶掉来源自己的现版。"""
    return (projected.get("facts") or {}).get("publication_role") or "source_record"


def _current(conn, source_id, is_demo, record_id, role="source_record"):
    """这条记录现在的版本（没有被 replaced_by 指走的最新一个）：version_id、fingerprint、原件和载荷。"""
    return conn.execute(text(
        "SELECT version_id, fingerprint, payload FROM history_versions "
        "WHERE source_id=:source AND is_demo=:demo AND source_record_id=:record AND replaced_by IS NULL "
        "AND COALESCE(payload->'facts'->>'publication_role','source_record')=:role "
        "ORDER BY published_seq DESC LIMIT 1"),
        {"source": source_id, "demo": is_demo, "record": record_id, "role": role},
    ).mappings().first()


def _fingerprint_of(source_id, current) -> str:
    # 加指纹列以前存的版本没有指纹（ops/backfill_fingerprint.py 补）：没补的当场按原件算
    return current["fingerprint"] or fingerprint(source_id, current["payload"]["raw_content"])


def stored_unchanged(source_id, item) -> bool:
    """增量读取用（fetch/transport.py 的 Budget.known）：这条原件库里已经有了、而且没变？"""
    with history_engine().begin() as conn:
        current = _current(conn, source_id, False, item.source_record_id)
    return current is not None and _fingerprint_of(source_id, current) == fingerprint(source_id, item.raw_content)


def _publish(conn, run, prepared, now, coverage, *, error=None, available_at=None):
    """把一次运行读到的原件存成版本（调用方的事务里），然后结束这次运行。prepared：[(原件, 投影)]。
    available_at：档案导入声明的公开可见时间（只给 public_history_reconstruction 模式用）。"""
    source_id, is_demo = run["source_id"], run["is_demo"]
    seq = _source(conn, source_id, lock=True)["published_seq"]
    version_ids, new_ids = [], []
    for item, projected in prepared:
        fp = fingerprint(source_id, item.raw_content)
        current = _current(conn, source_id, is_demo, item.source_record_id, _role(projected))
        if current is not None and _fingerprint_of(source_id, current) == fp:
            version_ids.append(current["version_id"])  # 没变：不存
            continue
        seq += 1
        version_id = sha256(f"{source_id}|{item.source_record_id}|{fp}|{seq}")
        _insert_version(conn, run, item, projected, version_id, fp, seq, now, available_at)
        if current is not None:
            conn.execute(text("UPDATE history_versions SET replaced_by=:new WHERE version_id=:old"),
                         {"new": version_id, "old": current["version_id"]})
        registry_changes.record(  # 临床登记：和上一版逐字段的差分，与新版本同一事务
            conn, source_id=source_id, is_demo=is_demo, record_id=item.source_record_id, previous=current,
            to_version_id=version_id, facts=projected.get("facts"), locators=projected.get("field_locators"),
            pairing="current_at_adoption", summary=projected.get("clean_text"), raw=item.raw_content)
        version_ids.append(version_id)
        new_ids.append(version_id)
    _link_attachments(conn, run, prepared, version_ids)
    conn.execute(text("UPDATE history_sources SET published_seq=:seq WHERE source_id=:id"),
                 {"seq": seq, "id": source_id})
    _close_run(conn, run, version_ids, new_ids, coverage, error, now)


def _insert_version(conn, run, item, projected, version_id, fp, seq, now, available_at):
    proof = AvailabilityProof(kind="declared_archive", available_at=available_at) if available_at else \
        AvailabilityProof(kind="unverified")
    payload = HistoryRecord(
        source_id=run["source_id"], source_record_id=item.source_record_id, version_id=version_id,
        is_demo=run["is_demo"], raw_content=item.raw_content, raw_media_type=item.raw_media_type,
        raw_sha256=sha256(item.raw_content), first_seen_at=now, received_at=now, availability_proof=proof,
        published_seq=seq, **projected,
    )
    conn.execute(text("""
        INSERT INTO history_versions (version_id,source_id,is_demo,source_record_id,record_kind,first_seen_at,
          received_at,proof_available_at,published_seq,fingerprint,payload)
        VALUES (:id,:source,:demo,:record,:kind,:now,:now,:proof,:seq,:fp,CAST(:payload AS jsonb))"""),
        {"id": version_id, "source": run["source_id"], "demo": run["is_demo"], "record": item.source_record_id,
         "kind": item.record_kind, "now": now, "proof": available_at.upper_bound() if available_at else None,
         "seq": seq, "fp": fp, "payload": payload.model_dump_json()})
    conn.execute(text(_DERIVE + " FROM history_versions WHERE version_id=:id"), {"id": version_id})


def _link_attachments(conn, run, prepared, version_ids):
    """附件版本 → 它的父页面版本。父页面在同一次读取里就记下它这次的版本；不在（例如只补抓了附件）就记父版本
    未知，不猜。同一组关系只记一次。"""
    by_record = {item.source_record_id: version for (item, _), version in zip(prepared, version_ids)}
    for (item, projected), version in zip(prepared, version_ids):
        facts = projected.get("facts") or {}
        if facts.get("role") != "attachment" or not facts.get("parent_record_id"):
            continue
        parent_version = by_record.get(facts["parent_record_id"])
        conn.execute(text("""
          INSERT INTO history_attachment_links (attachment_version_id,parent_record_id,parent_version_id,link_basis,run_id)
          SELECT :a,:p,:pv,:basis,:run WHERE NOT EXISTS (SELECT 1 FROM history_attachment_links
            WHERE attachment_version_id=:a AND parent_record_id=:p AND COALESCE(parent_version_id,'')=COALESCE(:pv,''))"""),
            {"a": version, "p": facts["parent_record_id"], "pv": parent_version, "run": run["id"],
             "basis": "same_run_parent_version" if parent_version else "parent_version_unknown"})


# 因为条数、页数或时间用完而停下的读取：来源没读完，状态记 partial
_LIMIT_STOPS = ("page_budget", "record_budget", "deadline")


def _close_run(conn, run, version_ids, new_ids, coverage, error, now):
    state = "partial" if error and version_ids else "failed" if error else "completed"
    if run["kind"] == "fetch" and not error and coverage.get("stop_reason") in _LIMIT_STOPS:
        state = "partial"
    coverage = {**coverage, "new_version_count": len(new_ids), "same_version_count": len(version_ids) - len(new_ids),
                "adopted_count": len(version_ids)}
    if "backfill" in coverage:  # 回填（ops/who_backfill.py）新存的版本是背景材料，不是新动态
        coverage["backfill"] = {**coverage["backfill"], "background_version_ids": new_ids}
    conn.execute(text("""
        UPDATE history_runs SET state=:state, completed_at=:now, version_ids=CAST(:ids AS jsonb),
          coverage=CAST(:coverage AS jsonb), error=CAST(:error AS jsonb) WHERE id=:id AND attempt=:attempt"""),
        {"state": state, "now": now, "ids": canonical(version_ids), "coverage": canonical(coverage),
         "error": canonical(error), "id": run["id"], "attempt": run["attempt"]})


def background_version_ids(conn) -> list[str]:
    """回填运行新存的版本（背景材料，clean/measure_runs.py 不测它们）。"""
    return [r[0] for r in conn.execute(text(
        "SELECT DISTINCT jsonb_array_elements_text(coverage->'backfill'->'background_version_ids') FROM history_runs "
        "WHERE jsonb_typeof(coverage->'backfill'->'background_version_ids') = 'array'"))]


# ---- 运行：采集任务和导入 ----------------------------------------------------------------------------------


def _view(row):
    values = {key: row[key] for key in RunView.model_fields if key in row}
    return RunView(**{**values, "id": str(row["id"])})


def _run(conn, run_id, *, lock=False):
    row = conn.execute(text("SELECT * FROM history_runs WHERE id=:id" + (" FOR UPDATE" if lock else "")),
                       {"id": run_id}).mappings().first()
    if row is None:
        raise HistoryError("run_not_found", "任务不存在", 404)
    return row


def read_run(run_id):
    with history_engine().begin() as conn:
        row = _run(conn, run_id)
        _source(conn, row["source_id"])
        return _view(row)


def _new_run(conn, source_id, is_demo, kind, body, key, scope):
    """新建一次运行，状态 queued。带了幂等键而且这个键用过：返回原来那次（fresh=False），不再建。"""
    _source(conn, source_id)
    key = key or str(uuid.uuid4())
    row = conn.execute(text("""
        INSERT INTO history_runs (id,source_id,is_demo,kind,state,idempotency_key,request_payload,scope_description)
        VALUES (:id,:source,:demo,:kind,'queued',:key,CAST(:body AS jsonb),:scope)
        ON CONFLICT (idempotency_key) DO NOTHING RETURNING *"""),
        {"id": str(uuid.uuid4()), "source": source_id, "demo": is_demo, "kind": kind, "key": key,
         "body": canonical(body), "scope": scope}).mappings().first()
    if row is not None:
        return row, True
    row = conn.execute(text("SELECT * FROM history_runs WHERE idempotency_key=:key"), {"key": key}).mappings().one()
    if (row["source_id"], row["kind"]) != (source_id, kind):
        raise HistoryError("idempotency_conflict", "这个幂等键已用于别的来源或别的任务", 409)
    return row, False


LEGACY_RETIRED = ("legacy_retired", "旧文档冻结桥 legacy_documents 已退役：不再写入，旧版本按旧编号仍可读")


def import_archive(request: ArchiveImport, key: str | None = None, *, graph_checked=False):
    """导入一包原件（场景回放、图谱发布、本地文件），和抓取一样存成版本。proof.available_at 给了的，这批新版本在
    public_history_reconstruction 模式下从那个时间起可见。图包在导入前由 graph/publish/history_graph.py 核对。"""
    if request.source_id == "legacy_documents":
        raise HistoryError(*LEGACY_RETIRED, 410)
    if request.source_id in GRAPH_SOURCES and not graph_checked:
        raise HistoryError("graph_import_unchecked", "图包要经 graph/publish/history_graph.py 的 import_graph 导入", 422)
    prepared = [(item, project_record(request.source_id, item)) for item in request.records]
    body = request.model_dump(mode="json")
    body["records"] = [{"source_record_id": r.source_record_id, "record_kind": r.record_kind}
                       for r in request.records]  # 原件已存在版本里，运行只记清单
    with history_engine().begin() as conn:
        run, fresh = _new_run(conn, request.source_id, request.is_demo, "archive", body, key,
                              request.scope_description)
        if fresh:
            coverage = {"declared_count": request.declared_count, "complete": request.declared_complete,
                        "stop_reason": "end_of_declared_manifest"}
            _publish(conn, run, prepared, _now(conn), coverage,
                     available_at=request.proof.available_at if request.proof else None)
        return _view(_run(conn, run["id"]))


def create_fetch(request: FetchRequest, key: str | None = None):
    """建一条采集任务（还没读）。返回 (运行, 是否新建)：幂等键用过就是原来那条。"""
    with history_engine().begin() as conn:
        row, fresh = _new_run(conn, request.source_id, False, "fetch", request.model_dump(mode="json", exclude_none=True),
                              key, "公开有界检索：" + request.query)
        return _view(row), fresh


def job_input(run_id, attempt) -> FetchRequest:
    """采集作业开始：任务从 queued 改成 running，返回要读的请求。"""
    with history_engine().begin() as conn:
        row = _run(conn, run_id, lock=True)
        if row["kind"] != "fetch" or row["state"] != "queued" or row["attempt"] != attempt:
            raise HistoryError("stale_attempt", "该任务已经在跑或跑完了", 409)
        _source(conn, row["source_id"])
        conn.execute(text("UPDATE history_runs SET state='running' WHERE id=:id"), {"id": run_id})
        return FetchRequest.model_validate(row["request_payload"])


# 读取结果里原样记进运行 coverage 的部分（不是每个读取插件都有）
REPORTED = ("complete", "upstream_pagination_exhausted", "stop_reason", "requests", "bytes", "source_total",
            "next_continuation", "attachment_coverage", "unread_items", "skipped_items", "window_coverage",
            "truncated_intervals", "backfill", "count_drift")


def finish_fetch(run_id, attempt, batch):
    """采集作业读完：把读到的原件存成版本，记下这次读取的情况（读到哪、为什么停、哪些没读到）。"""
    with history_engine().begin() as conn:
        run = _run(conn, run_id, lock=True)
        if run["attempt"] != attempt or run["state"] != "running":
            raise HistoryError("stale_attempt", "旧任务尝试不得存版本", 409)
        prepared = [(item, project_record(run["source_id"], item)) for item in batch["records"]]
        coverage = {key: batch.get(key) for key in REPORTED[:6]}
        coverage.update({key: batch[key] for key in REPORTED[6:] if batch.get(key)})
        for key in ("attachment_coverage", "unread_items", "skipped_items"):
            if key in coverage:
                coverage[key] = coverage[key][:500]
        _publish(conn, run, prepared, _now(conn), coverage, error=batch.get("error"))
        return _view(_run(conn, run_id))


def fail_job(run_id, attempt, code, message):
    with history_engine().begin() as conn:
        conn.execute(text("""
          UPDATE history_runs SET state='failed', completed_at=clock_timestamp(), error=CAST(:error AS jsonb)
          WHERE id=:id AND attempt=:attempt AND state IN ('queued','running')"""),
            {"id": run_id, "attempt": attempt, "error": canonical({"code": code, "message": message})})


# ---- 读 -----------------------------------------------------------------------------------------------


def _cutoff(value):
    try:
        return utc(value)
    except (ValueError, AttributeError):
        raise HistoryError("invalid_cutoff", "截止时间必须包含时区", 422) from None


def _eligibility(mode):
    """这个模式下，截止时间 :cutoff 时版本可见的条件。"""
    if mode in ("current", "operational_replay"):
        return "first_seen_at <= :cutoff"
    if mode == "public_history_reconstruction":
        return "proof_available_at IS NOT NULL AND proof_available_at <= :cutoff"
    raise HistoryError("unsupported_mode", "不支持该历史读取模式")


def _derived_mode(source_id, mode):
    """图谱和旧文档桥没有公开历史可见时间，不能按 public_history_reconstruction 读。"""
    if source_id in {"primekg_graph", "reviewed_graph", "legacy_documents"} and mode == "public_history_reconstruction":
        raise HistoryError("graph_history_unsupported", "图谱和旧文档桥没有公开历史可见时间，只能按首次看到的时间读")


def _open_read(conn, source_id, mode, cutoff):
    """每个按模式读的接口开头都一样：来源可读、模式支持、截止时间不晚于现在。返回 UTC 截止时间。"""
    _derived_mode(source_id, mode)
    source = _source(conn, source_id)
    cutoff = _cutoff(cutoff)
    if cutoff > _now(conn):
        raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
    return source, cutoff


# 一条记录"在截止时间下可见的版本"：先按模式和截止时间筛，再排第一。排序：来源自己的版本先于 legacy 迁入的；
# 再看最晚的公开可见/首次看到时间，来源自己的更新时间，最后看发布序号。排序键在 history_version_rank
# （V021 以前的版本没有那一行，就从载荷里现算，结果一样）。
_SOURCE_UPDATED_FROM_PAYLOAD = """CASE payload->'source_updated_at'->>'precision'
 WHEN 'instant' THEN (payload->'source_updated_at'->>'value')::timestamptz
 WHEN 'day' THEN (((payload->'source_updated_at'->>'value')::date + interval '1 day' - interval '1 microsecond') AT TIME ZONE 'UTC')
 WHEN 'month' THEN ((((payload->'source_updated_at'->>'value') || '-01')::date + interval '1 month' - interval '1 microsecond') AT TIME ZONE 'UTC')
 WHEN 'year' THEN ((((payload->'source_updated_at'->>'value') || '-01-01')::date + interval '1 year' - interval '1 microsecond') AT TIME ZONE 'UTC')
 ELSE NULL END"""
_ROLE_FROM_PAYLOAD = "COALESCE(payload->'facts'->>'publication_role','source_record')"
_VERSIONS = "history_versions LEFT JOIN history_version_rank USING (version_id)"
_ROLE = "COALESCE(publication_role," + _ROLE_FROM_PAYLOAD + ")"
_SOURCE_UPDATED_ORDER = (
    "(CASE WHEN publication_role IS NULL THEN " + _SOURCE_UPDATED_FROM_PAYLOAD + " ELSE source_updated_rank END)"
)
_LATEST = (
    "(" + _ROLE + "='migrated_record'),"
    "COALESCE(proof_available_at,first_seen_at) DESC," + _SOURCE_UPDATED_ORDER + " DESC NULLS LAST,published_seq DESC"
)
_DERIVE = (
    "INSERT INTO history_version_rank(version_id,publication_role,source_updated_rank) SELECT version_id,"
    + _ROLE_FROM_PAYLOAD + "," + _SOURCE_UPDATED_FROM_PAYLOAD
)


def _ranked(where, mode):
    """WITH 子句：符合 where、在截止时间下可见的版本，每条记录按 _LATEST 编号；n=1 就是此刻可见的那个。"""
    return (
        "WITH eligible AS (SELECT * FROM " + _VERSIONS + " WHERE " + where + " AND " + _eligibility(mode)
        + " AND " + _ROLE + "!='context_reference'), ranked AS (SELECT *,row_number() OVER "
        "(PARTITION BY source_record_id ORDER BY " + _LATEST + ") n FROM eligible) "
    )


def backfill_rank(source_id, *, batch=500):
    """给 V021 以前存的版本补排序键（ops/backfill_history_rank.py），一次最多 batch 个；返回补了几个。"""
    with history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '30000ms'"))
        return conn.execute(text(
            _DERIVE + " FROM history_versions WHERE version_id IN (SELECT h.version_id FROM history_versions h "
            "LEFT JOIN history_version_rank r ON r.version_id=h.version_id "
            "WHERE h.source_id=:source AND r.version_id IS NULL LIMIT :batch)"),
            {"source": source_id, "batch": batch}).rowcount


# 整源字面检索（ILIKE 扫正文）只在不大的来源上做；大来源请用 POST /api/history/search（WeKnora 检索）
LITERAL_SEARCH_MAX_VERSIONS = 20000


def _read_filters(conn, source, is_demo, cutoff, max_seq, run_id, ids, terms, kind):
    """list_records 的筛选条件 (where, 参数, 字面检索条件)。run_id：只读这次运行存下（或用到）的那些记录。"""
    if terms and not ids and not run_id and source["published_seq"] > LITERAL_SEARCH_MAX_VERSIONS:
        raise HistoryError("query_budget", f"该来源有 {source['published_seq']} 个版本，整源字面检索太慢；"
                           "请用 POST /api/history/search，或给出记录编号、运行编号", 422)
    if len(ids) > 5000 or len(terms) > 3 or any(not 2 <= len(term) <= 120 for term in terms):
        raise HistoryError("query_budget", "记录编号最多 5000 个，检索词最多 3 个、每个 2–120 字")
    where = ["source_id=:source", "is_demo=:demo", "published_seq<=:max_seq"]
    params = {"source": source["source_id"], "demo": is_demo, "cutoff": cutoff, "max_seq": max_seq}
    if run_id:
        run = _run(conn, run_id)
        if run["source_id"] != source["source_id"] or run["is_demo"] != is_demo:
            raise HistoryError("manifest_mismatch", "运行与来源或 demo 类别不匹配")
        if run["state"] not in ("completed", "partial", "failed"):
            raise HistoryError("manifest_not_ready", "运行还没结束", 409)
        where.append("source_record_id IN (SELECT source_record_id FROM history_versions "
                     "WHERE version_id=ANY(CAST(:run_versions AS text[])))")
        params["run_versions"] = list(run["version_ids"])
    if ids:
        where.append("source_record_id=ANY(CAST(:ids AS text[]))")
        params["ids"] = sorted(set(ids))
    if kind:
        where.append("record_kind=:kind")
        params["kind"] = kind
    term_clauses = []
    for index, term in enumerate(sorted(set(terms))):
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term_clauses.append(f"(payload->>'title' ILIKE :term{index} ESCAPE '\\' "
                            f"OR payload->>'clean_text' ILIKE :term{index} ESCAPE '\\')")
        params[f"term{index}"] = "%" + escaped + "%"
    return " AND ".join(where), params, " AND ".join(term_clauses) or "true"


# GDELT 线索按它自己的观察时间（source_updated_at，meaning gdelt_first_observed）分窗口读；读每个窗口时留下回执
# （fetch/gdelt_news.py 写进运行的 coverage.window_coverage）。按窗口列线索时，把这些回执一并给出。
LEAD_WINDOW_SOURCES = ("gdelt_doc",)
MAX_WINDOW_RUNS = 200


def _lead_window(source_id, window):
    """线索窗口 (起, 止) 的 UTC 时间，不按窗口读时为 None。"""
    if window == (None, None):
        return None
    try:
        bounds = (utc(window[0]), utc(window[1]))
    except (ValueError, AttributeError, TypeError):
        bounds = None
    if source_id not in LEAD_WINDOW_SOURCES or bounds is None or bounds[0] >= bounds[1]:
        raise HistoryError("invalid_window", "线索窗口须给出含时区的起止时间（起点早于终点），且只适用于 GDELT", 422)
    return bounds


def _window_receipts(conn, source_id, is_demo, cutoff, window):
    """这个窗口里各次读取留下的回执：截止时间前结束、读取范围碰到这个窗口的运行；同一个检索词同一个区间，
    以最近一次为准（前一次没读到的，后一次读了）。返回 (回执, 可能没读全的区间)。"""
    rows = conn.execute(text(
        "SELECT request_payload->>'query' AS query, coverage FROM history_runs "
        "WHERE source_id=:source AND is_demo=:demo AND kind='fetch' AND state IN ('completed','partial','failed') "
        "AND completed_at<=:cutoff AND jsonb_typeof(coverage->'window_coverage')='array' "
        "AND COALESCE((request_payload->>'updated_until')::timestamptz,completed_at)>:start "
        "AND COALESCE((request_payload->>'updated_since')::timestamptz,completed_at-interval '24 hours')<:end "
        "ORDER BY completed_at,id LIMIT :limit"),
        {"source": source_id, "demo": is_demo, "cutoff": cutoff, "start": window[0], "end": window[1],
         "limit": MAX_WINDOW_RUNS + 1}).mappings().all()
    if len(rows) > MAX_WINDOW_RUNS:
        raise HistoryError("window_too_wide", f"窗口内的读取超过 {MAX_WINDOW_RUNS} 次，请缩小窗口", 422)
    latest = {}
    for row in rows:
        for receipt in row["coverage"]["window_coverage"]:
            start, end = receipt.get("start"), receipt.get("end")
            if start is None or end is None or (parse_timestamp(start) < window[1] and parse_timestamp(end) > window[0]):
                latest[(row["query"], start, end)] = receipt
    receipts = list(latest.values())
    return receipts, [{"start": r.get("start"), "end": r.get("end")} for r in receipts if r.get("possibly_truncated")]


def _page(rows, limit):
    """一页的版本（不超过响应字节上限）和后面还有没有。"""
    items, size = [], 0
    for row in rows[:limit]:
        record = HistoryRecord.model_validate(row["payload"])
        size += len(record.model_dump_json().encode())
        if size > MAX_RESPONSE_BYTES:
            break
        items.append(record)
    if rows[:limit] and not items:
        raise HistoryError("response_budget", "单条来源记录超过响应上限", 413)
    return items, len(rows) > len(items)


def list_records(source_id, is_demo, mode, cutoff, *, run_id=None, source_record_ids=(), terms=(), record_kind=None,
                 limit=25, cursor=None, window=(None, None)):
    """GET /api/history/records：每条记录在截止时间下可见的版本，按记录号排序分页。cursor 是上一页给的
    "发布序号上界|最后一个记录号"，原样传回即可。"""
    if source_id == "legacy_documents" and not source_record_ids:
        raise HistoryError(*LEGACY_RETIRED, 410)  # 按旧编号读不受影响
    if not 1 <= limit <= 100:
        raise HistoryError("query_budget", "每页条数须为1至100")
    window = _lead_window(source_id, window)
    with history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        source, cutoff = _open_read(conn, source_id, mode, cutoff)
        max_seq, last = source["published_seq"], ""
        if cursor:
            try:
                max_seq, last = int(cursor.split("|", 1)[0]), cursor.split("|", 1)[1]
            except (ValueError, IndexError):
                raise HistoryError("invalid_cursor", "分页游标无效", 422) from None
        where, params, term_filter = _read_filters(conn, source, is_demo, cutoff, max_seq, run_id,
                                                   source_record_ids, terms, record_kind)
        if window:
            where += " AND " + _SOURCE_UPDATED_ORDER + ">=:window_start AND " + _SOURCE_UPDATED_ORDER + "<:window_end"
            params.update(window_start=window[0], window_end=window[1])
        rows = conn.execute(text(
            _ranked(where, mode) + "SELECT payload FROM ranked WHERE n=1 AND source_record_id>:last AND "
            + term_filter + " ORDER BY source_record_id LIMIT :limit"),
            {**params, "last": last, "limit": limit + 1}).mappings().all()
        items, more = _page(rows, limit)
        coverage = {"max_seq": max_seq, "has_more": more}
        if window:
            coverage["window_coverage"], coverage["truncated_intervals"] = _window_receipts(
                conn, source_id, is_demo, cutoff, window)
        return RecordCollection(items=items, coverage=coverage,
                                next_cursor=f"{max_seq}|{items[-1].source_record_id}" if more else None)


def read_version(version_id, source_id, source_record_id, is_demo, mode, cutoff):
    """GET /api/history/versions/{version_id}：这个固定版本，只要它在这个模式和截止时间下可见。"""
    if not re.fullmatch(r"[0-9a-f]{64}", version_id):
        raise HistoryError("version_not_found", "版本不存在", 404)
    with history_engine().begin() as conn:
        _, cutoff = _open_read(conn, source_id, mode, cutoff)
        row = conn.execute(text(
            "SELECT payload FROM history_versions WHERE version_id=:id AND source_id=:source "
            "AND source_record_id=:record AND is_demo=:demo AND " + _eligibility(mode)),
            {"id": version_id, "source": source_id, "record": source_record_id, "demo": is_demo, "cutoff": cutoff},
        ).mappings().first()
        if row is None:
            raise HistoryError("version_not_found", "该版本在当前来源、范围或截止时间下不可见", 404)
        record = HistoryRecord.model_validate(row["payload"])
        if source_id == "legacy_documents":
            record = _with_legacy_alias(conn, record, mode, cutoff)
        return record


def _with_legacy_alias(conn, record, mode, cutoff):
    """按旧编号读 legacy 版本：版本原样，附上它迁成了哪个真实来源的哪个版本（legacy_aliases，fetch/legacy_migration.py）。"""
    from fetch.legacy_migration import alias

    site = (record.facts or {}).get("source_site")
    if site:
        _source(conn, site)  # 原站不可读，旧版本也不可读
    info = alias(conn, record.version_id, mode, cutoff)
    return record.model_copy(update={"migration": info}) if info else record


def visible_versions(source_id, is_demo, mode, cutoff, source_record_ids):
    """{记录号: 在这个模式和截止时间下可见的版本号}（store/retrieval.py 核对检索命中用）；不可见的不在结果里。"""
    ids = sorted(set(source_record_ids))
    if not 1 <= len(ids) <= 200:
        raise HistoryError("query_budget", "一次最多核对 200 条记录")
    with history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        source, cutoff = _open_read(conn, source_id, mode, cutoff)
        where, params, _ = _read_filters(conn, source, is_demo, cutoff, source["published_seq"], None, ids, (), None)
        rows = conn.execute(text(_ranked(where, mode) + "SELECT source_record_id,version_id FROM ranked WHERE n=1"),
                            params).mappings()
        return {row["source_record_id"]: row["version_id"] for row in rows}


def list_changes(source_id, is_demo, *, after=0, limit=25):
    """GET /api/history/changes：发布序号 after 之后新存的版本，按序号。雷达用它跟新增：下次从 next_after 接着读。"""
    if after < 0 or not 1 <= limit <= 100:
        raise HistoryError("query_budget", "变更读取参数不合法")
    with history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        max_seq = _source(conn, source_id)["published_seq"]
        rows = conn.execute(text(
            "SELECT payload FROM history_versions WHERE source_id=:source AND is_demo=:demo AND published_seq>:after "
            "AND published_seq<=:max_seq ORDER BY published_seq LIMIT :limit"),
            {"source": source_id, "demo": is_demo, "after": after, "max_seq": max_seq, "limit": limit + 1},
        ).mappings().all()
        items, more = _page(rows, limit)
        return {"schema_version": "datafabric.history.changes.v1",
                "items": [item.model_dump(mode="json") for item in items], "max_seq": max_seq,
                "next_after": items[-1].published_seq if more else max_seq}


def version_differences(source_id, is_demo, version_ids, mode, cutoff):
    """GET /api/history/version-differences：每个版本的上一版。版本只在原件指纹变了时才新存，所以有上一版就是
    原件变了（kind=raw_changed）；没有上一版是这条记录第一次存（first_adoption）；看不到的是 not_visible。"""
    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    with history_engine().begin() as conn:
        _, cutoff = _open_read(conn, source_id, mode, cutoff)
        visible = _eligibility(mode).replace("proof_available_at", "v.proof_available_at").replace(
            "first_seen_at", "v.first_seen_at")
        previous = _eligibility(mode).replace("proof_available_at", "p.proof_available_at").replace(
            "first_seen_at", "p.first_seen_at")
        rows = conn.execute(text(
            "SELECT v.version_id, v.source_record_id, p.version_id AS previous FROM history_versions v "
            "LEFT JOIN history_versions p ON p.replaced_by=v.version_id AND " + previous + " "
            "WHERE v.version_id=ANY(:ids) AND v.source_id=:source AND v.is_demo=:demo AND " + visible),
            {"ids": list(version_ids), "source": source_id, "demo": is_demo, "cutoff": cutoff}).mappings().all()
    found = {row["version_id"]: row for row in rows}

    def answer(version_id):
        row = found.get(version_id)
        if row is None:
            return {"state": "not_visible"}
        if row["previous"] is None:
            return {"state": "first_adoption", "source_record_id": row["source_record_id"]}
        return {"state": "compared", "source_record_id": row["source_record_id"], "from_version_id": row["previous"],
                "kind": "raw_changed"}

    return {"schema_version": "datafabric.history.version_differences.v1", "source_id": source_id, "mode": mode,
            "as_of": cutoff.isoformat(), "versions": {v: answer(v) for v in version_ids}}
