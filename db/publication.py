"""原采集事务发表待办与有界补发；跨库只用冻结输入和history既有幂等键。"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from biz_rules.collected_document import CollectedDocument, collected_document
from biz_rules.history_models import (
    ArchiveImport,
    ArchiveRecord,
    HistoryError,
    canonical,
    sha256,
)
from biz_rules.reviewed_graph import (
    GraphBundle,
    GraphDocumentRef,
    LegacyDocument,
    document_native_id,
)
from db import history


def publication_engine():
    url = os.environ.get("DATAFABRIC_PUBLICATION_DATABASE_URL", "")
    if not url:
        raise HistoryError(
            "publication_not_configured", "原采集发表数据库须显式配置", 503
        )
    return history._engine(url)


def source_sites():
    try:
        sites = json.loads(os.environ.get("DATAFABRIC_PUBLICATION_SOURCES", "[]"))
        if (
            not isinstance(sites, list)
            or not sites
            or any(not isinstance(value, str) or not value.strip() for value in sites)
        ):
            raise ValueError()
        return sorted(set(sites))
    except ValueError:
        raise HistoryError(
            "publication_sources_not_configured", "须明确原站点发表许可清单", 503
        ) from None


def is_demo():
    value = os.environ.get("DATAFABRIC_PUBLICATION_IS_DEMO", "false")
    if value not in {"true", "false"}:
        raise HistoryError(
            "publication_demo_config", "测试标志须由服务配置为true或false", 503
        )
    return value == "true"


def enqueue_changed_document(session, envelope):
    """旧缓存不能覆盖；新清洗正文仍在同一原库事务中可靠排入待发表。"""
    exists = session.execute(
        text("SELECT to_regclass('publication_outbox') IS NOT NULL")
    ).scalar()
    if exists is not True:
        raise RuntimeError("同一来源正文发生变化，旧结果已保留；请经版本化历史入口采用")
    snapshot = {
        "uuid": envelope.uuid,
        "title": envelope.title or "",
        "raw_content": envelope.raw_content or "",
        "source_site": envelope.provenance.source_site or "",
        "source_url": envelope.provenance.source_url or "",
        "publish_date": envelope.publish_date or "",
        "is_deleted": False,
        "created_at": None,
        "updated_at": None,
    }
    session.execute(
        text(
            "SELECT publication_record_document(CAST(:value AS jsonb),'changed_body')"
        ),
        {"value": canonical(snapshot)},
    )
    pending = session.execute(
        text(
            "SELECT state FROM publication_outbox WHERE kind='document' AND source_doc_uuid=:uuid ORDER BY id DESC LIMIT 1"
        ),
        {"uuid": envelope.uuid},
    ).scalar_one()
    return {
        "publication_pending": pending != "published",
        "legacy_cache_retained": True,
    }


def disk_status():
    path = os.environ.get(
        "DATAFABRIC_PUBLICATION_DISK_PATH", "/home/zk_test/zk_projects"
    )
    try:
        minimum = int(
            os.environ.get("DATAFABRIC_PUBLICATION_MIN_FREE_BYTES", str(15 * 1024**3))
        )
        if minimum < 0:
            raise ValueError()
        free = shutil.disk_usage(path).free
    except (ValueError, OSError):
        raise HistoryError(
            "publication_disk_unknown", "无法核对磁盘安全余量；存量回填暂停", 503
        ) from None
    return {
        "free_bytes": free,
        "minimum_free_bytes": minimum,
        "backfill_paused": free < minimum,
    }


def enqueue_current_document(session, uuid):
    """每次成功采集都比较候选状态；原缓存相同也可能是B之后恢复到A。"""
    if session.execute(
        text("SELECT to_regclass('publication_outbox') IS NOT NULL")
    ).scalar():
        session.flush()
        session.execute(
            text(
                "SELECT publication_record_document(to_jsonb(d),'update') FROM documents d WHERE uuid=:uuid"
            ),
            {"uuid": uuid},
        )


def backfill_once(engine=None, *, limit=50):
    """首次存量有固定id上界；游标和待办同事务提交，新插入由触发器负责。"""
    engine = engine or publication_engine()
    sites = source_sites()
    disk = disk_status()
    if disk["backfill_paused"]:
        return {
            "seeded": 0,
            "complete": False,
            "state": "paused_disk_low",
            "disk": disk,
            "scope": "initial_inventory_not_completed",
        }
    with history.history_engine().begin() as permission:
        history._source(permission, "legacy_documents")
        for site in sites:
            history._source(permission, site)
    if not 1 <= limit <= 100:
        raise HistoryError("publication_budget", "存量补发每批1至100篇")
    key = "backfill:" + sha256(canonical(sites))
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='5000ms'"))
        # 先消化可处理的新提交；隔离/失败仍由发表器重试，不能永久卡住其它库存。
        if conn.execute(
            text("""
              SELECT EXISTS(
                SELECT 1 FROM publication_outbox o
                WHERE o.kind IN ('document','graph')
                  AND o.state IN ('pending','processing')
                  AND NOT EXISTS (
                    SELECT 1 FROM publication_outbox earlier
                    WHERE earlier.source_doc_uuid=o.source_doc_uuid
                      AND earlier.kind IN ('document','graph')
                      AND earlier.id<o.id AND earlier.state!='published'
                  )
              )
            """)
        ).scalar_one():
            return {
                "seeded": 0,
                "complete": False,
                "state": "pending_first",
                "scope": "initial_inventory_not_completed",
            }
        upper = conn.execute(
            text("SELECT COALESCE(max(id),0) FROM documents")
        ).scalar_one()
        conn.execute(
            text("""
          INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
          VALUES (:key,'backfill',:key,CAST(:snapshot AS jsonb)) ON CONFLICT(event_key) DO NOTHING
        """),
            {
                "key": key,
                "snapshot": canonical(
                    {"last_id": 0, "max_id": upper, "source_sites": sites}
                ),
            },
        )
        control = (
            conn.execute(
                text(
                    "SELECT * FROM publication_outbox WHERE event_key=:key FOR UPDATE"
                ),
                {"key": key},
            )
            .mappings()
            .one()
        )
        scope = control["snapshot"]
        if control["state"] == "published":
            return {
                "seeded": 0,
                "complete": True,
                "max_id": scope["max_id"],
                "scope": "initial_authorized_inventory",
            }
        rows = list(
            conn.execute(
                text("""
          SELECT id,to_jsonb(d) AS document FROM documents d
          WHERE id>:last AND id<=:upper AND source_site=ANY(:sites)
          ORDER BY id LIMIT :limit
        """),
                {
                    "last": scope["last_id"],
                    "upper": scope["max_id"],
                    "sites": sites,
                    "limit": limit + 1,
                },
            ).mappings()
        )
        picked = rows[:limit]
        seeded = 0
        for row in picked:
            # 与采集候选使用同一事务锁；等并发新版本提交后重新检查，不能把读到的旧库存排在它后面。
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:uuid,0))"),
                {"uuid": row["document"]["uuid"]},
            )
            if conn.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM publication_outbox WHERE kind='document' AND source_doc_uuid=:uuid)"
                ),
                {"uuid": row["document"]["uuid"]},
            ).scalar_one():
                continue
            conn.execute(
                text(
                    "SELECT publication_record_document(CAST(:value AS jsonb),'backfill')"
                ),
                {"value": canonical(row["document"])},
            )
            seeded += 1
            conn.execute(
                text("""
              INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
              SELECT :key,'graph',:uuid,CAST(:snapshot AS jsonb)
              WHERE EXISTS(SELECT 1 FROM extracted_entities WHERE source_doc_uuid=:uuid
                AND NOT is_deleted AND review_status IN ('approved','auto_approved'))
                 OR EXISTS(SELECT 1 FROM entity_relations WHERE source_doc_uuid=:uuid
                AND NOT is_deleted AND review_status IN ('approved','auto_approved'))
              ON CONFLICT(event_key) DO NOTHING
            """),
                {
                    "key": "graph:backfill:" + row["document"]["uuid"],
                    "uuid": row["document"]["uuid"],
                    "snapshot": canonical(
                        {
                            "source_doc_uuid": row["document"]["uuid"],
                            "capture_reason": "initial_graph_backfill",
                        }
                    ),
                },
            )
        more = len(rows) > limit
        scope = {**scope, "last_id": picked[-1]["id"] if more else scope["max_id"]}
        conn.execute(
            text("""
          UPDATE publication_outbox SET snapshot=CAST(:scope AS jsonb),
          state=:state,completed_at=CASE WHEN :state='published' THEN clock_timestamp() ELSE NULL END
          WHERE id=:id
        """),
            {
                "scope": canonical(scope),
                "state": "pending" if more else "published",
                "id": control["id"],
            },
        )
        return {
            "seeded": seeded,
            "scanned": len(picked),
            "complete": not more,
            "max_id": scope["max_id"],
            "scope": "initial_authorized_inventory_not_complete_source",
        }


def _claim(engine, *, prepared_only=False):
    token = uuid4().hex
    with engine.begin() as conn:
        # 失败的较旧正文会阻止同一文档后来事件超车，但不阻止其它文档。
        row = (
            conn.execute(
                text("""
          SELECT o.* FROM publication_outbox o
          WHERE o.kind IN ('document','graph') AND o.state!='published'
           AND o.next_attempt_at<=clock_timestamp()
           AND (NOT :prepared_only OR o.prepared IS NOT NULL)
           AND (o.state!='processing' OR o.lease_until<clock_timestamp())
           AND NOT EXISTS (SELECT 1 FROM publication_outbox p
             WHERE p.source_doc_uuid=o.source_doc_uuid AND p.kind IN ('document','graph')
               AND p.id<o.id AND p.state!='published')
          ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT 1
        """),
                {"prepared_only": prepared_only},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        conn.execute(
            text("""
          UPDATE publication_outbox SET state='processing',attempts=attempts+1,
          lease_token=:token,lease_until=clock_timestamp()+interval '5 minutes' WHERE id=:id
        """),
            {"token": token, "id": row["id"]},
        )
        return {**row, "lease_token": token, "attempts": row["attempts"] + 1}


def _save_prepared(engine, row, prepared):
    with engine.begin() as conn:
        result = conn.execute(
            text("""
          UPDATE publication_outbox SET prepared=CAST(:prepared AS jsonb)
          WHERE id=:id AND state='processing' AND lease_token=:token
        """),
            {
                "prepared": canonical(prepared),
                "id": row["id"],
                "token": row["lease_token"],
            },
        )
        if result.rowcount != 1:
            raise HistoryError(
                "publication_lease_lost", "发表资格已变化，请由当前处理者继续", 409
            )
    row["prepared"] = prepared


def _graph_input(conn, uuid, fallback=None):
    value = conn.execute(
        text("SELECT to_jsonb(d) FROM documents d WHERE uuid=:uuid"), {"uuid": uuid}
    ).scalar_one_or_none()
    value = value or fallback
    if value is None:
        raise HistoryError(
            "graph_source_deleted",
            "原文已硬删除；文本撤回已单独记录，图当前不可再导出",
            410,
        )
    entities = list(
        conn.execute(
            text("""
      SELECT to_jsonb(e) AS item FROM extracted_entities e WHERE source_doc_uuid=:uuid
       AND NOT is_deleted AND review_status IN ('approved','auto_approved') ORDER BY id LIMIT 101
    """),
            {"uuid": uuid},
        ).scalars()
    )
    relations = list(
        conn.execute(
            text("""
      SELECT to_jsonb(e) AS item FROM entity_relations e WHERE source_doc_uuid=:uuid
       AND NOT is_deleted AND review_status IN ('approved','auto_approved') ORDER BY id LIMIT 201
    """),
            {"uuid": uuid},
        ).scalars()
    )
    ids = [
        item["dict_id"] for item in entities[:100] if item.get("dict_id") is not None
    ]
    verified = (
        list(
            conn.execute(
                text(
                    "SELECT id::text FROM entity_dict WHERE id=ANY(:ids) AND is_verified AND NOT is_deleted"
                ),
                {"ids": ids},
            ).scalars()
        )
        if ids
        else []
    )
    observed = conn.execute(text("SELECT clock_timestamp()")).scalar_one().isoformat()
    if value.get("is_deleted") is True:
        entities, relations, verified = [], [], []
    return {
        "document": value,
        "observed_at": observed,
        "observation": {
            "schema_version": "datafabric.legacy.graph.observation.v1",
            "namespace": os.environ.get(
                "DATAFABRIC_PUBLICATION_NAMESPACE", "datafabric.zktest"
            ),
            "selected_document_uuids": [uuid],
            "entities": entities[:100],
            "relations": relations[:200],
            "verified_dict_ids": verified,
            "scope_record_ids": [uuid],
            "scope_description": "自动发表所选单文档当前审核投影；不是完整图或每次中间审核历史",
            "selection_truncated": len(entities) > 100 or len(relations) > 200,
            "is_demo": is_demo(),
            "merge_logs": [],
        },
    }


def _prepare(engine, row):
    if row["prepared"] is not None:
        prepared = row["prepared"]
        if prepared["document_request"]["is_demo"] is not is_demo():
            raise HistoryError(
                "publication_demo_scope_changed",
                "冻结待办与当前真实/教学服务配置不符，未重新标记",
                403,
            )
        original = json.loads(prepared["document_request"]["records"][0]["raw_content"])
        if original["source_site"] not in source_sites():
            raise HistoryError(
                "publication_source_blocked", "该原站点已停用发表，冻结待办保留", 403
            )
        return prepared
    if row["kind"] == "document":
        frozen = row["snapshot"]["document"]
        reason = row["snapshot"]["capture_reason"]
        inputs = None
    else:
        with engine.begin() as conn:
            # 同一读取事务使用repeatable-read，图/文不能夹杂两个已提交状态。
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            conn.execute(text("SET LOCAL statement_timeout='5000ms'"))
            inputs = _graph_input(
                conn, row["source_doc_uuid"], row["snapshot"].get("document")
            )
        frozen, reason = inputs["document"], "graph_reference"
    if frozen.get("source_site") not in source_sites():
        raise HistoryError(
            "publication_source_blocked",
            "原站点尚未列入发表许可；待办和旧版已保留",
            403,
        )
    observed_at = inputs["observed_at"] if inputs is not None else row["created_at"]
    document = collected_document(frozen, observed_at, reason)
    native = document_native_id(document)
    kind = "trial" if document.source_projection is not None else "document"
    request = ArchiveImport(
        source_id="legacy_documents",
        is_demo=is_demo(),
        scope_description="实际清洗落库的单文档冻结版本；不是全源完整队列",
        declared_count=1,
        declared_complete=False,
        records=[
            ArchiveRecord(
                source_record_id=native,
                record_kind=kind,
                raw_content=document.model_dump_json(),
            )
        ],
    )
    existing = None
    if inputs is not None:
        with history.history_engine().begin() as conn:
            history._source(conn, "legacy_documents")
            existing = conn.execute(
                text("""
              SELECT payload FROM history_versions WHERE source_id='legacy_documents'
               AND source_record_id=:native AND is_demo=:demo
               AND payload->'facts'->>'original_raw_sha256'=:sha
               AND payload->'facts'->>'publication_state'=:state
               ORDER BY published_seq DESC LIMIT 1
            """),
                {
                    "native": native,
                    "demo": request.is_demo,
                    "sha": document.original_raw_sha256,
                    "state": document.publication_state,
                },
            ).scalar_one_or_none()
    prepared = {
        "document_request": request.model_dump(mode="json"),
        "graph_input": inputs,
        "existing_document_version": existing,
        "idempotency_prefix": "auto-publication:"
        + sha256(
            os.environ.get("DATAFABRIC_PUBLICATION_NAMESPACE", "datafabric.zktest")
            + "\n"
            + row["event_key"]
        ),
    }
    _save_prepared(engine, row, prepared)
    return prepared


def _publish_graph(engine, row, prepared, document_run):
    from flows.graph_publish import build_legacy_bundle

    if prepared.get("graph_request") is not None:
        return history.import_archive(
            ArchiveImport.model_validate(prepared["graph_request"]),
            prepared["idempotency_prefix"] + ":graph",
        )
    request = ArchiveImport.model_validate(prepared["document_request"])
    document = CollectedDocument.model_validate_json(request.records[0].raw_content)
    version_ids = (
        list(document_run.version_ids)
        if document_run is not None
        else [prepared["existing_document_version"]["version_id"]]
    )
    version = history.read_version(
        version_ids[0],
        "legacy_documents",
        request.records[0].source_record_id,
        request.is_demo,
        "current",
        datetime.now(timezone.utc),
    )
    ref = GraphDocumentRef(
        source_id="legacy_documents",
        source_record_id=version.source_record_id,
        version_id=version.version_id,
        raw_sha256=version.raw_sha256,
        origin_doc_uuid=document.source_doc_uuid,
    )
    if document.publication_state == "withdrawn":
        bundle = GraphBundle(
            graph_kind="reviewed",
            namespace=prepared["graph_input"]["observation"]["namespace"],
            is_demo=request.is_demo,
            title="源记录已撤回的当前图状态",
            origin="legacy_reviewed_export",
            scope_description="原文已删除；当前对象/关系为空，不是临床失败",
            selected_document_uuids=[document.source_doc_uuid],
            scope_record_ids=[document.source_doc_uuid],
            dependency_source_ids=["legacy_documents", document.source_site],
            document_refs=[ref],
            nodes=[],
            edges=[],
            redirects=[],
            limitations=["源记录已撤回，旧图版本仅保留历史观察"],
        )
    else:
        original = LegacyDocument(
            source_doc_uuid=document.source_doc_uuid,
            source_site=document.source_site,
            title=document.title,
            source_url=document.source_url,
            original_raw_content=document.original_raw_content,
            original_raw_sha256=document.original_raw_sha256,
        )
        observation = prepared["graph_input"]["observation"]
        bundle = build_legacy_bundle(observation, [original], [ref])

        # 清洗可能移除模板头/HTML，原MD偏移不能切clean_text。只接受原句的唯一精确匹配。
        def bound_evidence(items):
            from biz_rules.reviewed_graph import GraphLocator

            bound = []
            for evidence in items:
                start = version.clean_text.find(evidence.quote)
                if (
                    start >= 0
                    and version.clean_text.find(evidence.quote, start + 1) < 0
                ):
                    bound.append(
                        evidence.model_copy(
                            update={
                                "locator": GraphLocator(
                                    start=start, end=start + len(evidence.quote)
                                )
                            }
                        )
                    )
            return bound

        for node in bundle.nodes:
            node.evidence = bound_evidence(node.evidence)
        for edge in bundle.edges:
            edge.evidence = bound_evidence(edge.evidence)
            if not edge.evidence:
                edge.binding_status = "unbound"
                edge.unbound_reason = (
                    "来源句子在已采用叙述中缺失或多处出现，未作模糊定位"
                )
    graph_request = ArchiveImport(
        source_id="reviewed_graph",
        is_demo=request.is_demo,
        scope_description=bundle.scope_description,
        declared_count=1,
        declared_complete=False,
        records=[
            ArchiveRecord(
                source_record_id="auto:"
                + bundle.namespace
                + ":"
                + document.source_doc_uuid,
                record_kind="graph",
                raw_content=bundle.model_dump_json(),
            )
        ],
    )
    prepared = {**prepared, "graph_request": graph_request.model_dump(mode="json")}
    _save_prepared(engine, row, prepared)
    return history.import_archive(
        graph_request, prepared["idempotency_prefix"] + ":graph"
    )


def _known_commits(row):
    """低磁盘时只准确认已成功发表的固定请求，不能开始任何新原件复制。"""
    prepared = row["prepared"]
    if not prepared:
        return False
    required = []
    if prepared.get("existing_document_version") is None:
        required.append(("document", prepared["document_request"]))
    if row["kind"] == "graph":
        if not prepared.get("graph_request"):
            return False
        required.append(("graph", prepared["graph_request"]))
    with history.history_engine().begin() as conn:
        for kind, request in required:
            saved = (
                conn.execute(
                    text(
                        "SELECT state,request_hash FROM history_runs WHERE idempotency_key=:key"
                    ),
                    {"key": prepared["idempotency_prefix"] + ":" + kind},
                )
                .mappings()
                .first()
            )
            if (
                not saved
                or saved["state"] not in {"completed", "partial"}
                or saved["request_hash"] != sha256(canonical(request))
            ):
                return False
    return True


def publish_once(engine=None):
    engine = engine or publication_engine()
    disk = disk_status()
    row = _claim(engine, prepared_only=disk["backfill_paused"])
    if row is None:
        return (
            {"state": "resource_blocked", "reason": "disk_low", "disk": disk}
            if disk["backfill_paused"]
            else {"state": "idle"}
        )
    try:
        if disk["backfill_paused"] and not _known_commits(row):
            raise HistoryError(
                "publication_resource_blocked",
                "磁盘低水位，暂停新原件发表并保留待办",
                503,
            )
        prepared = _prepare(engine, row)
        if prepared.get("existing_document_version") is not None:
            result = None
            receipt = {
                "document_run_id": None,
                "document_version_ids": [
                    prepared["existing_document_version"]["version_id"]
                ],
            }
        else:
            result = history.import_archive(
                ArchiveImport.model_validate(prepared["document_request"]),
                prepared["idempotency_prefix"] + ":document",
            )
            receipt = {
                "document_run_id": result.id,
                "document_version_ids": list(result.version_ids),
            }
        if row["kind"] == "graph":
            graph = _publish_graph(engine, row, prepared, result)
            receipt.update(
                graph_run_id=graph.id, graph_version_ids=list(graph.version_ids)
            )
        with engine.begin() as conn:
            updated = conn.execute(
                text("""
              UPDATE publication_outbox SET state='published',receipt=CAST(:receipt AS jsonb),
                completed_at=clock_timestamp(),lease_until=NULL,error=NULL,prepared=NULL,
                snapshot=jsonb_build_object('source_doc_uuid',source_doc_uuid,
                  'content_fingerprint',snapshot->'content_fingerprint',
                  'snapshot_sha256',encode(sha256(convert_to(snapshot::text,'UTF8')),'hex'))
              WHERE id=:id AND lease_token=:token AND state='processing'
            """),
                {
                    "receipt": canonical(receipt),
                    "id": row["id"],
                    "token": row["lease_token"],
                },
            )
            if updated.rowcount != 1:
                raise HistoryError(
                    "publication_lease_lost",
                    "已发表但确认资格变化；相同固定输入将安全核对",
                    409,
                )
        return {"state": "published", "id": row["id"], **receipt}
    except (HistoryError, SQLAlchemyError, ValueError, RuntimeError) as exc:
        # 已冻结输入和同key允许跨库提交未知后核对；绝不重新抓取变更内容。
        blocked = isinstance(exc, HistoryError) and exc.status in {403, 410}
        code = exc.code if isinstance(exc, HistoryError) else "publication_failed"
        message = (
            exc.message
            if isinstance(exc, HistoryError)
            else "发表未完成；待办及旧版本保留，将按固定输入核对"
        )
        with engine.begin() as conn:
            conn.execute(
                text("""
              UPDATE publication_outbox SET state=:state,error=CAST(:error AS jsonb),
                lease_until=NULL,next_attempt_at=clock_timestamp()+interval '30 seconds'
              WHERE id=:id AND lease_token=:token AND state='processing'
            """),
                {
                    "state": "pending"
                    if code == "publication_resource_blocked"
                    else "blocked"
                    if blocked
                    else "failed",
                    "error": canonical({"code": code, "message": message}),
                    "id": row["id"],
                    "token": row["lease_token"],
                },
            )
        return {
            "state": "resource_blocked"
            if code == "publication_resource_blocked"
            else "blocked"
            if blocked
            else "failed",
            "id": row["id"],
            "error": {"code": code, "message": message},
        }


def publisher_heartbeat(state, *, engine=None):
    with (engine or publication_engine()).begin() as conn:
        conn.execute(
            text("""
          INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
          VALUES ('publisher-runtime','control','publisher-runtime',
            jsonb_build_object('last_heartbeat',clock_timestamp(),'publisher_state',:state))
          ON CONFLICT(event_key) DO UPDATE SET snapshot=EXCLUDED.snapshot
        """),
            {"state": state},
        )


def publication_status(source_id="legacy_documents", engine=None):
    if source_id not in {"legacy_documents", "reviewed_graph"}:
        raise HistoryError("publication_status_scope", "仅查询自动文档或审核图发表组")
    with history.history_engine().begin() as permit:
        history._source(permit, source_id)
    kind = "document" if source_id == "legacy_documents" else "graph"
    with (engine or publication_engine()).begin() as conn:
        counts = {
            row["state"]: row["count"]
            for row in conn.execute(
                text(
                    "SELECT state,count(*) AS count FROM publication_outbox WHERE kind=:kind GROUP BY state"
                ),
                {"kind": kind},
            ).mappings()
        }
        backfills = [
            {"state": row["state"], **row["snapshot"]}
            for row in conn.execute(
                text(
                    "SELECT state,snapshot FROM publication_outbox WHERE kind='backfill' ORDER BY id"
                )
            ).mappings()
        ]
        heartbeat_row = (
            conn.execute(
                text(
                    "SELECT snapshot,(snapshot->>'last_heartbeat')::timestamptz AS last_at "
                    "FROM publication_outbox WHERE event_key='publisher-runtime'"
                )
            )
            .mappings()
            .first()
        )
        heartbeat = heartbeat_row["snapshot"] if heartbeat_row else {}
        last_at = heartbeat_row["last_at"] if heartbeat_row else None
        now = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        database_name = conn.execute(text("SELECT current_database()")).scalar_one()
    disk = disk_status()
    last = last_at.isoformat() if last_at else None
    state = heartbeat.get("publisher_state", "not_started")
    if last_at and (now - last_at).total_seconds() > 120:
        state = "stale"
    if disk["backfill_paused"]:
        state = "resource_blocked"
    return {
        "schema_version": "datafabric.publication.status.v1",
        "is_demo": is_demo(),
        "namespace": os.environ.get(
            "DATAFABRIC_PUBLICATION_NAMESPACE", "datafabric.zktest"
        ),
        "source_database": database_name,
        "source_id": source_id,
        "observed_at": now.isoformat(),
        "last_heartbeat": last,
        "publisher_state": state,
        "counts": counts,
        "disk": {
            "free_bytes": disk["free_bytes"],
            "minimum_free_bytes": disk["minimum_free_bytes"],
            "paused": disk["backfill_paused"],
        },
        "backfill": backfills,
        "limitations": [
            "状态仅指本机自动发表，不证明原站全覆盖；库存回填与新写入分开",
            "暂停/失败保留待办；没有changes不证明源生产器或发表器正常",
        ],
    }
