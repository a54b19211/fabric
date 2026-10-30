"""历史资料的三张表读写；短事务发布，原件和版本只追加。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from functools import lru_cache
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text

from biz_rules.history_models import (
    MAX_RESPONSE_BYTES,
    PARSER_VERSION,
    ArchiveImport,
    AvailabilityProof,
    FetchRequest,
    HistoryError,
    HistoryRecord,
    MappingReview,
    RecordCollection,
    RunView,
    TimeValue,
    archive_manifest_hash,
    canonical,
    sha256,
    source_time,
    utc,
)
from biz_rules.history_sources import project_record


@lru_cache(maxsize=2)
def _engine(url):
    return create_engine(
        url,
        pool_size=3,
        max_overflow=2,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )


def history_engine():
    # 绝不回退到旧APP.DB_URL，防止独立工作树误连现役库。
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL", "")
    if not url:
        raise HistoryError("history_not_configured", "历史资料数据库尚未配置", 503)
    return _engine(url)


def allowed_sources():
    value = os.environ.get("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", "")
    try:
        values = json.loads(value) if value.startswith("[") else value.split(",")
    except ValueError:
        raise HistoryError("history_not_configured", "来源许可配置无效", 503) from None
    return {str(item).strip() for item in values if str(item).strip()}


def _catalog_dependencies(row):
    values = row["catalog"].get("dependency_source_ids", [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) for value in values
    ):
        raise HistoryError("invalid_source_dependencies", "派生来源许可配置无效", 503)
    return set(values)


def _source(conn, source_id, *, lock=False, _seen=None):
    seen = set() if _seen is None else set(_seen)
    if source_id in seen:
        raise HistoryError("cyclic_source_dependencies", "来源许可依赖存在循环", 503)
    seen.add(source_id)
    if source_id not in allowed_sources():
        raise HistoryError("source_not_allowed", "当前许可不允许该来源", 403)
    row = (
        conn.execute(
            text(
                "SELECT * FROM history_sources WHERE source_id=:id"
                + (" FOR UPDATE" if lock else "")
            ),
            {"id": source_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["read_enabled"] is not True:
        raise HistoryError("source_not_available", "该来源当前不可读取", 403)
    for dependency in _catalog_dependencies(row):
        _source(conn, dependency, _seen=seen)
    return row


def list_sources():
    with history_engine().begin() as conn:
        rows = list(
            conn.execute(
                text("SELECT * FROM history_sources ORDER BY source_id")
            ).mappings()
        )
        by_id, allowed = {row["source_id"]: row for row in rows}, allowed_sources()

        def enabled(source_id, seen=()):
            row = by_id.get(source_id)
            if (
                row is None
                or source_id in seen
                or source_id not in allowed
                or row["read_enabled"] is not True
            ):
                return False
            return all(
                enabled(value, (*seen, source_id))
                for value in _catalog_dependencies(row)
            )

        return [
            {
                "source_id": row["source_id"],
                **row["catalog"],
                "published_seq": row["published_seq"],
                "read_enabled": True,
            }
            for row in rows
            if enabled(row["source_id"])
        ]


def _derived_mode(source_id, mode):
    if source_id == "primekg_graph" and mode == "public_history_reconstruction":
        raise HistoryError(
            "graph_history_unsupported",
            "PrimeKG捕获没有外部历史可得证明；本地回放只从first_seen开始",
        )
    if (
        source_id in {"reviewed_graph", "legacy_documents"}
        and mode == "public_history_reconstruction"
    ):
        raise HistoryError(
            "graph_history_unsupported", "当前图/旧文档导出没有外部历史可得证明"
        )


def _validate_derived(conn, run, item, now, proof):
    from biz_rules.history_sources import parse_json
    from biz_rules.reviewed_graph import DERIVED_SOURCES, GraphBundle, LegacyDocument

    if run["source_id"] == "external_archive":
        data = parse_json(item.raw_content)
        if data.get("schema_version") == "datafabric.observation.v1":
            return _validate_observation(conn, run, data, now)
    if run["source_id"] not in DERIVED_SOURCES:
        return set()
    if proof.kind != "unverified" or proof.available_at is not None:
        raise HistoryError(
            "graph_history_unsupported", "当前图和旧文档桥不能借导入证明回填过去"
        )
    data = parse_json(item.raw_content)
    if run["source_id"] == "legacy_documents":
        if data.get("schema_version") == "datafabric.collected.document.v1":
            from biz_rules.collected_document import CollectedDocument

            original = CollectedDocument.model_validate(data)
        else:
            original = LegacyDocument.model_validate(data)
        _source(conn, original.source_site)
        return {original.source_site}

    bundle = GraphBundle.model_validate(data)
    if bundle.is_demo != run["is_demo"]:
        raise HistoryError("graph_demo_mismatch", "图包和采用请求的demo标记不一致")
    if run["source_id"] == "primekg_graph":
        if bundle.graph_kind != "background":
            raise HistoryError("graph_kind_mismatch", "PrimeKG只能发布当前背景")
        return set()
    if bundle.graph_kind != "reviewed":
        raise HistoryError("graph_kind_mismatch", "审核图来源不接收背景图")

    if bundle.origin == "legacy_reviewed_export" and (
        not bundle.selected_document_uuids
        or set(bundle.selected_document_uuids)
        != {ref.origin_doc_uuid for ref in bundle.document_refs}
    ):
        raise HistoryError(
            "legacy_graph_document_scope", "旧图导出的所选UUID须全部绑定精确冻结文档"
        )
    documents, dependencies = {}, set()
    for ref in bundle.document_refs:
        _source(conn, ref.source_id)
        row = conn.execute(
            text(
                "SELECT payload FROM history_versions WHERE version_id=:id AND source_id=:source AND source_record_id=:native AND is_demo=:demo AND first_seen_at<=:now"
            ),
            {
                "id": ref.version_id,
                "source": ref.source_id,
                "native": ref.source_record_id,
                "demo": run["is_demo"],
                "now": now,
            },
        ).scalar_one_or_none()
        if (
            row is None
            or row["raw_sha256"] != ref.raw_sha256
            or row["record_kind"] == "graph"
        ):
            raise HistoryError(
                "graph_evidence_version_mismatch", "图包原件引用与已采用文档版本不一致"
            )
        if ref.source_id == "legacy_documents":
            if row["facts"].get("origin_doc_uuid") != ref.origin_doc_uuid:
                raise HistoryError(
                    "graph_document_uuid_mismatch", "图引用的旧文档UUID不一致"
                )
            original_source = row["facts"]["source_site"]
            _source(conn, original_source)
            dependencies.add(original_source)
        dependencies.add(ref.source_id)
        key = (
            ref.source_id,
            ref.source_record_id,
            ref.version_id,
            ref.raw_sha256,
            ref.origin_doc_uuid,
        )
        if key in documents:
            raise HistoryError("duplicate_graph_document", "图包文档清单重复")
        documents[key] = row
    if dependencies != set(bundle.dependency_source_ids):
        raise HistoryError(
            "graph_source_dependencies_mismatch",
            "图包依赖必须等于服务器核对的原件及原站许可",
        )
    for node in bundle.nodes:
        if node.review.reviewed_at is not None and node.review.reviewed_at > now:
            raise HistoryError("future_graph_review", "源审核时间晚于本次服务器采用")
    for edge in bundle.edges:
        if edge.review.reviewed_at is not None and edge.review.reviewed_at > now:
            raise HistoryError("future_graph_review", "源审核时间晚于本次服务器采用")
    for ref in [value for node in bundle.nodes for value in node.evidence] + [
        value for edge in bundle.edges for value in edge.evidence
    ]:
        key = (
            ref.source_id,
            ref.source_record_id,
            ref.version_id,
            ref.raw_sha256,
            ref.origin_doc_uuid,
        )
        original = documents.get(key)
        if original is None:
            raise HistoryError(
                "graph_evidence_outside_manifest", "图证据不属于本包声明的文档版本"
            )
        locator = ref.locator
        if original["clean_text"][
            locator.start : locator.end
        ] != ref.quote or locator.end > len(original["clean_text"]):
            raise HistoryError(
                "graph_quote_mismatch", "图证据与冻结原文字符范围不一致，不能标记已绑定"
            )
    if any(
        item.merged_at is not None and item.merged_at > now for item in bundle.redirects
    ):
        raise HistoryError("future_graph_merge", "源身份合并时间晚于本次服务器采用")
    from biz_rules.native_graph import validate_field_bindings

    validate_field_bindings(bundle, documents)
    return dependencies


def _validate_observation(conn, run, data, now):
    from biz_rules.observations import (
        native_json,
        observation,
        same_series,
        validate_bound_values,
    )

    value = observation(data)
    dependencies = set()
    for purpose, ref in (("basis", value.basis.ref), ("supersedes", value.supersedes)):
        if ref is None:
            continue
        _source(conn, ref.source_id)
        row = conn.execute(
            text("""
            SELECT payload FROM history_versions WHERE source_id=:source
              AND source_record_id=:native AND version_id=:version AND is_demo=:demo
              AND first_seen_at<=:now
        """),
            {
                "source": ref.source_id,
                "native": ref.source_record_id,
                "version": ref.version_id,
                "demo": run["is_demo"],
                "now": now,
            },
        ).scalar_one_or_none()
        if row is None or row["raw_sha256"] != ref.raw_sha256:
            raise HistoryError(
                "observation_ref_mismatch", "观测引用与同空间冻结版本不一致"
            )
        try:
            original = native_json(row["raw_content"])
            if purpose == "basis":
                validate_bound_values(value, original)
            elif not same_series(value, observation(original)):
                raise ValueError("修订不能替换不同对象或口径")
        except ValueError:
            raise HistoryError(
                "observation_binding_mismatch", "观测值、口径或修订引用与原件不一致"
            ) from None
        if ref.source_id != run["source_id"]:
            dependencies.add(ref.source_id)
    return dependencies


def _persist_dependencies(conn, source, dependencies):
    combined = _catalog_dependencies(source) | dependencies
    if source["source_id"] in combined:
        raise HistoryError("cyclic_source_dependencies", "派生来源不能依赖自身")
    if combined != _catalog_dependencies(source):
        conn.execute(
            text(
                "UPDATE history_sources SET catalog=jsonb_set(catalog,'{dependency_source_ids}',CAST(:deps AS jsonb),true) WHERE source_id=:id"
            ),
            {"deps": canonical(sorted(combined)), "id": source["source_id"]},
        )


def read_graph_neighborhood(
    version_id,
    source_id,
    source_record_id,
    is_demo,
    mode,
    cutoff,
    *,
    seed_ids,
    relation_types=(),
    direction="both",
    max_depth=1,
    max_nodes=50,
    max_edges=100,
    review_scope=None,
):
    from biz_rules.history_sources import parse_json
    from biz_rules.reviewed_graph import (
        GRAPH_SOURCES,
        GraphBundle,
        GraphIdentity,
        GraphRef,
        neighborhood,
    )

    if source_id not in GRAPH_SOURCES:
        raise HistoryError("unsupported_graph_source", "请选择固定图来源")
    record = read_version(
        version_id, source_id, source_record_id, is_demo, mode, cutoff
    )
    if record.record_kind != "graph":
        raise HistoryError("not_a_graph_version", "所选版本不是图包")
    seeds = []
    for value in seed_ids:
        if value.count("|") != 1:
            raise HistoryError("invalid_graph_identity", "身份格式须为namespace|id")
        namespace, identity = value.split("|")
        try:
            seeds.append(GraphIdentity(namespace=namespace, id=identity))
        except ValueError:
            raise HistoryError(
                "invalid_graph_identity", "身份命名空间或ID无效"
            ) from None
    bundle = GraphBundle.model_validate(parse_json(record.raw_content))
    return neighborhood(
        bundle,
        GraphRef(
            source_id=record.source_id,
            source_record_id=record.source_record_id,
            version_id=record.version_id,
            raw_sha256=record.raw_sha256,
            is_demo=record.is_demo,
            first_seen_at=record.first_seen_at,
        ),
        seeds,
        relation_types=relation_types,
        direction=direction,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        review_scope=review_scope,
    )


def _view(row):
    values = {key: row[key] for key in RunView.model_fields}
    values["id"] = str(values["id"])
    return RunView(**values)


def read_run(run_id):
    with history_engine().begin() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id"), {"id": run_id}
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HistoryError("run_not_found", "采用任务不存在", 404)
        _source(conn, row["source_id"])
        return _view(row)


def _now(conn):
    return conn.execute(text("SELECT clock_timestamp()")).scalar_one()


def _new_run(conn, source_id, is_demo, kind, body, key, scope, stored_body=None):
    if (
        not key
        or len(key) > 160
        or any(ord(char) < 33 or ord(char) > 126 for char in key)
    ):
        raise HistoryError(
            "idempotency_key_required", "请提供1至160字符的Idempotency-Key", 400
        )
    _source(conn, source_id)
    request_hash = sha256(canonical(body))
    run_id = str(uuid.uuid4())
    row = (
        conn.execute(
            text("""
        INSERT INTO history_runs
          (id,source_id,is_demo,kind,state,request_hash,idempotency_key,request_payload,
           scope_description,job_id)
        VALUES (:id,:source,:demo,:kind,'queued',:hash,:key,CAST(:body AS jsonb),:scope,:job)
        ON CONFLICT (idempotency_key) DO NOTHING RETURNING *
    """),
            {
                "id": run_id,
                "source": source_id,
                "demo": is_demo,
                "kind": kind,
                "hash": request_hash,
                "key": key,
                "body": canonical(stored_body if stored_body is not None else body),
                "scope": scope,
                "job": "history-" + run_id + "-1" if kind == "fetch" else None,
            },
        )
        .mappings()
        .first()
    )
    if row is not None:
        return row, True
    row = (
        conn.execute(
            text("SELECT * FROM history_runs WHERE idempotency_key=:key"), {"key": key}
        )
        .mappings()
        .one()
    )
    if row["request_hash"] != request_hash or row["kind"] != kind:
        raise HistoryError("idempotency_conflict", "同一幂等键已用于不同采用请求", 409)
    _source(conn, row["source_id"])
    return row, False


def _proof(request: ArchiveImport, now):
    proof = request.proof
    if proof is None:
        return AvailabilityProof(kind="unverified")
    if proof.sha256 != archive_manifest_hash(request.records):
        raise HistoryError("archive_hash_mismatch", "归档证明未绑定本包全部原件")
    if proof.available_at.upper_bound() > now:
        raise HistoryError("future_archive_proof", "归档可见证明不能晚于当前服务器时间")
    parsed = urlsplit(proof.uri)
    if parsed.scheme not in (
        ("https", "http", "fixture") if request.is_demo else ("https",)
    ):
        raise HistoryError(
            "invalid_archive_proof",
            "真实历史证明须提供可核对HTTPS出处；fixture仅限demo",
        )
    if parsed.username or parsed.password or not parsed.netloc:
        raise HistoryError("invalid_archive_proof", "归档证明地址不可含凭据")
    if proof.reviewed and not proof.review_note.strip():
        raise HistoryError(
            "missing_review_basis", "审核历史归档须说明原件、许可和可得日期依据"
        )
    return AvailabilityProof(
        kind="reviewed_archive" if proof.reviewed else "unverified",
        available_at=proof.available_at,
        uri=proof.uri,
        sha256=proof.sha256,
        verified_by="history-admin" if proof.reviewed else None,
        verified_at=now if proof.reviewed else None,
        review_note=proof.review_note if proof.reviewed else None,
    )


def import_archive(request: ArchiveImport, key: str):
    # 全包先验证再发布；任何一条非法均不部分提交或改变已有成果。
    prepared = [
        (item, project_record(request.source_id, item)) for item in request.records
    ]
    with history_engine().begin() as conn:
        now = _now(conn)
        proof = _proof(request, now)
        body = request.model_dump(mode="json")
        stored = {
            **body,
            "records": [
                {
                    "source_record_id": item.source_record_id,
                    "record_kind": item.record_kind,
                    "raw_sha256": sha256(item.raw_content),
                }
                for item in request.records
            ],
        }
        row, fresh = _new_run(
            conn,
            request.source_id,
            request.is_demo,
            "archive",
            body,
            key,
            request.scope_description,
            stored_body=stored,
        )
        if not fresh:
            return _view(row)
        coverage = {
            "mode": "declared_manifest",
            "complete": request.declared_complete,
            "upstream_pagination_exhausted": None,
            "declared_count": request.declared_count,
            "adopted_count": len(prepared),
            "declared_scope_count": len(request.scope_record_ids or request.records),
            "stop_reason": "end_of_declared_manifest",
            "limitations": ["完整仅指所声明归档包，不证明原站/领域全覆盖"],
        }
        _publish(
            conn,
            row,
            prepared,
            proof,
            now,
            coverage,
            verified_mapping_ids=set(request.verified_mapping_ids),
            mapping_basis=request.mapping_review_basis,
        )
        final = (
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id"), {"id": row["id"]}
            )
            .mappings()
            .one()
        )
        return _view(final)


def _publish(
    conn,
    run,
    prepared,
    proof,
    now,
    coverage,
    *,
    error=None,
    verified_mapping_ids=frozenset(),
    mapping_basis="",
):
    source = _source(conn, run["source_id"], lock=True)
    seq = source["published_seq"]
    version_ids, new_count = [], 0
    dependencies = set()
    for item, projected in prepared:
        dependencies.update(_validate_derived(conn, run, item, now, proof))
        mapping_review = None
        if item.source_record_id in verified_mapping_ids:
            facts = projected["facts"]
            required = (
                "program_id",
                "lineage_id",
                "indication",
                "jurisdiction",
                "mapping_available_at",
            )
            if not all(
                isinstance(facts.get(key), str) and facts[key].strip()
                for key in required
            ):
                raise HistoryError(
                    "mapping_incomplete", "审核项目映射缺少五项有出处字段"
                )
            mapping_time = source_time(facts["mapping_available_at"])
            if mapping_time is None or mapping_time.upper_bound() > now:
                raise HistoryError(
                    "invalid_mapping_time", "映射公开时间格式错误或晚于本次采用"
                )
            mapping_review = MappingReview(
                reviewed_by="history-admin",
                reviewed_at=now,
                basis=mapping_basis,
                available_at=proof.available_at,
            )
        if proof.kind == "reviewed_archive":
            # 后续结局/新映射不能借旧原件证明倒灌到历史特征。
            bounds = [
                projected.get("source_published_at"),
                projected.get("source_updated_at"),
            ]
            if projected["record_kind"] == "outcome":
                facts = projected["facts"]
                bounds += [
                    source_time(facts.get(key))
                    for key in ("event_at", "public_available_at", "observation_end")
                ]
            if mapping_review:
                bounds.append(mapping_time)
            for bound in bounds:
                value = (
                    TimeValue.model_validate(bound)
                    if isinstance(bound, dict)
                    else bound
                )
                if value and value.upper_bound() > proof.available_at.upper_bound():
                    raise HistoryError(
                        "archive_time_conflict",
                        "原件新增事实、结局窗口或映射晚于归档可见证明",
                    )
        if proof.kind == "reviewed_archive":
            prior_versions = conn.execute(
                text("""
                  SELECT payload FROM history_versions
                  WHERE source_id=:source AND is_demo=:demo AND source_record_id=:native
                    AND proof_available_at IS NOT NULL
                """),
                {
                    "source": run["source_id"],
                    "demo": run["is_demo"],
                    "native": item.source_record_id,
                },
            ).scalars()
            updated = (
                TimeValue.model_validate(projected["source_updated_at"])
                if projected["source_updated_at"]
                else None
            )
            for previous in prior_versions:
                if previous["raw_sha256"] == sha256(item.raw_content):
                    continue
                old_proof = TimeValue.model_validate(
                    previous["availability_proof"]["available_at"]
                )
                current_proof = proof.available_at
                overlap = (
                    current_proof.lower_bound() <= old_proof.upper_bound()
                    and old_proof.lower_bound() <= current_proof.upper_bound()
                )
                if not overlap:
                    continue
                if current_proof.upper_bound() != old_proof.upper_bound():
                    raise HistoryError(
                        "ambiguous_history_version",
                        "历史可见证明的时间精度重叠，不能确定修订先后；请补精确档案证明，旧版已保留",
                    )
                old_updated = (
                    TimeValue.model_validate(previous["source_updated_at"])
                    if previous.get("source_updated_at")
                    else None
                )
                ordered = (
                    updated is not None
                    and old_updated is not None
                    and (
                        updated.upper_bound() < old_updated.lower_bound()
                        or old_updated.upper_bound() < updated.lower_bound()
                    )
                )
                if not ordered:
                    raise HistoryError(
                        "ambiguous_history_version",
                        "同一历史时点的原件无法确定修订先后；请补可区分的来源修订时间或档案证明，旧版已保留",
                    )
        proof_data = proof.model_dump(mode="json")
        # 本次审核/采用时钟不参与版本身份；重复提交同原件/同审核依据复用原版本。
        identity_proof = {k: v for k, v in proof_data.items() if k != "verified_at"}
        identity_mapping = (
            mapping_review.model_dump(mode="json") if mapping_review else None
        )
        if identity_mapping:
            identity_mapping.pop("reviewed_at")
        identity = {
            "source_id": run["source_id"],
            "source_record_id": item.source_record_id,
            "is_demo": run["is_demo"],
            "raw_sha256": sha256(item.raw_content),
            "raw_media_type": item.raw_media_type,
            "parser_version": PARSER_VERSION,
            **projected,
            "availability_proof": identity_proof,
            "mapping_review": identity_mapping,
        }
        version_id = sha256(canonical(identity))
        existing = conn.execute(
            text("SELECT version_id FROM history_versions WHERE version_id=:id"),
            {"id": version_id},
        ).first()
        if run["kind"] == "fetch" and existing is not None:
            # 公开观察只把“与当前相同”当重复；A→B→A是一次新的采用。
            # 档案导入仍按原件/证明去重，不把迟到的旧档案改成当前状态。
            current = conn.execute(
                text(
                    "SELECT version_id,payload FROM history_versions "
                    "WHERE source_id=:source AND is_demo=:demo AND source_record_id=:native "
                    "AND COALESCE(payload->'facts'->>'publication_role','source_record')!='context_reference' "
                    "ORDER BY COALESCE(proof_available_at,first_seen_at) DESC,"
                    + _SOURCE_UPDATED_ORDER
                    + " DESC NULLS LAST,published_seq DESC LIMIT 1"
                ),
                {"source": run["source_id"], "demo": run["is_demo"],
                 "native": item.source_record_id},
            ).mappings().first()
            if current is not None:
                prior_identity = {key: current["payload"].get(key) for key in identity}
                prior_identity["availability_proof"] = {
                    key: value for key, value in prior_identity["availability_proof"].items()
                    if key != "verified_at"
                }
                if prior_identity["mapping_review"]:
                    prior_identity["mapping_review"] = {
                        key: value for key, value in prior_identity["mapping_review"].items()
                        if key != "reviewed_at"
                    }
                if canonical(prior_identity) == canonical(identity):
                    # 恢复后的连续A复用A3，不能重新引用A1或再增加一个版本。
                    version_id = current["version_id"]
                else:
                    # 首次出现的内容ID不变；再次采用旧内容时，以前一版区分这次变化。
                    version_id = sha256(canonical({
                        "content_version_id": version_id,
                        "previous_version_id": current["version_id"],
                    }))
                    existing = conn.execute(
                        text("SELECT version_id FROM history_versions WHERE version_id=:id"),
                        {"id": version_id},
                    ).first()
        if existing is None:
            seq += 1
            payload = HistoryRecord(
                source_id=run["source_id"],
                source_record_id=item.source_record_id,
                version_id=version_id,
                is_demo=run["is_demo"],
                raw_content=item.raw_content,
                raw_media_type=item.raw_media_type,
                raw_sha256=identity["raw_sha256"],
                first_seen_at=now,
                received_at=now,
                availability_proof=proof,
                mapping_review=mapping_review,
                published_seq=seq,
                **projected,
            )
            proof_at = (
                proof.available_at.upper_bound()
                if proof.kind == "reviewed_archive"
                else None
            )
            conn.execute(
                text("""
                INSERT INTO history_versions
                  (version_id,source_id,is_demo,source_record_id,record_kind,first_seen_at,received_at,
                   proof_available_at,published_seq,payload)
                VALUES (:id,:source,:demo,:native,:kind,:first,:received,:proof,:seq,CAST(:payload AS jsonb))
            """),
                {
                    "id": version_id,
                    "source": run["source_id"],
                    "demo": run["is_demo"],
                    "native": item.source_record_id,
                    "kind": item.record_kind,
                    "first": now,
                    "received": now,
                    "proof": proof_at,
                    "seq": seq,
                    "payload": payload.model_dump_json(),
                },
            )
            new_count += 1
        version_ids.append(version_id)
    _persist_dependencies(conn, source, dependencies)
    conn.execute(
        text("UPDATE history_sources SET published_seq=:seq WHERE source_id=:id"),
        {"seq": seq, "id": run["source_id"]},
    )
    state = "partial" if error and version_ids else "failed" if error else "completed"
    if (
        run["kind"] == "fetch"
        and not error
        and coverage.get("stop_reason") in ("page_budget", "record_budget", "deadline")
    ):
        state = "partial"
    coverage = {
        **coverage,
        "new_version_count": new_count,
        "same_version_count": len(version_ids) - new_count,
        "adopted_count": len(version_ids),
    }
    attempts = list(run["attempts"]) + [
        {
            "attempt": run["attempt"],
            "state": state,
            "completed_at": now.isoformat(),
            "version_ids": version_ids,
            "error": error,
        }
    ]
    conn.execute(
        text("""
        UPDATE history_runs SET state=:state,completed_at=:now,version_ids=CAST(:ids AS jsonb),
          coverage=CAST(:coverage AS jsonb),error=CAST(:error AS jsonb),attempts=CAST(:attempts AS jsonb),
          manifest_sha256=:manifest WHERE id=:id AND attempt=:attempt
    """),
        {
            "state": state,
            "now": now,
            "ids": canonical(version_ids),
            "coverage": canonical(coverage),
            "error": canonical(error),
            "attempts": canonical(attempts),
            "manifest": archive_manifest_hash([item for item, _ in prepared]),
            "id": run["id"],
            "attempt": run["attempt"],
        },
    )


def create_fetch(request: FetchRequest, key: str):
    with history_engine().begin() as conn:
        row, fresh = _new_run(
            conn,
            request.source_id,
            False,
            "fetch",
            request.model_dump(mode="json", exclude_none=True),
            key,
            "公开有界检索：" + request.query,
        )
        return _view(row), fresh


def job_input(run_id, attempt):
    with history_engine().begin() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id FOR UPDATE"),
                {"id": run_id},
            )
            .mappings()
            .first()
        )
        if (
            row is None
            or row["kind"] != "fetch"
            or row["state"] != "queued"
            or row["attempt"] != attempt
        ):
            raise HistoryError("stale_attempt", "该任务尝试已失效，不重新发布", 409)
        _source(conn, row["source_id"])
        conn.execute(
            text("UPDATE history_runs SET state='running' WHERE id=:id"), {"id": run_id}
        )
        return FetchRequest.model_validate(row["request_payload"])


def finish_fetch(run_id, attempt, batch):
    with history_engine().begin() as conn:
        run = (
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id FOR UPDATE"),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        if run["attempt"] != attempt or run["state"] != "running":
            raise HistoryError("stale_attempt", "旧任务尝试不得发布", 409)
        prepared = [
            (item, project_record(run["source_id"], item)) for item in batch["records"]
        ]
        coverage = {
            key: batch[key]
            for key in (
                "complete",
                "upstream_pagination_exhausted",
                "stop_reason",
                "requests",
                "bytes",
                "source_total",
            )
        }
        if batch.get("next_continuation") is not None:
            coverage["next_continuation"] = batch["next_continuation"]
        coverage.update(
            mode="bounded_public_query",
            limitations=[
                "有界公开检索不是完整历史队列；不据此训练成功项目筛选后的伪队列"
            ],
        )
        _publish(
            conn,
            run,
            prepared,
            AvailabilityProof(kind="unverified"),
            _now(conn),
            coverage,
            error=batch["error"],
        )
        return _view(
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id"), {"id": run_id}
            )
            .mappings()
            .one()
        )


def fail_job(run_id, attempt, code, message):
    with history_engine().begin() as conn:
        conn.execute(
            text("""
          UPDATE history_runs SET state='failed',completed_at=clock_timestamp(),
            error=CAST(:error AS jsonb)
          WHERE id=:id AND attempt=:attempt AND state IN ('queued','running')
        """),
            {
                "id": run_id,
                "attempt": attempt,
                "error": canonical({"code": code, "message": message}),
            },
        )


def enqueue_failed(run_id, attempt):
    with history_engine().begin() as conn:
        conn.execute(
            text("""
          UPDATE history_runs SET state='enqueue_failed',error=CAST(:error AS jsonb)
          WHERE id=:id AND attempt=:attempt AND state='queued'
        """),
            {
                "id": run_id,
                "attempt": attempt,
                "error": canonical(
                    {
                        "code": "enqueue_unknown",
                        "message": "队列提交结果未知；先核对固定job_id，再显式恢复",
                    }
                ),
            },
        )


def retry_fetch(run_id, expected_attempt, *, job_inactive):
    if not job_inactive:
        raise HistoryError("job_may_be_active", "原任务可能仍在执行，不能重发", 409)
    with history_engine().begin() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id FOR UPDATE"),
                {"id": run_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HistoryError("run_not_found", "任务不存在", 404)
        _source(conn, row["source_id"])
        if (
            row["kind"] != "fetch"
            or row["attempt"] != expected_attempt
            or row["state"] == "completed"
        ):
            raise HistoryError(
                "retry_conflict", "任务类型、状态或尝试版本不允许恢复", 409
            )
        attempt = expected_attempt + 1
        history = list(row["attempts"])
        if not any(item["attempt"] == expected_attempt for item in history):
            history.append(
                {
                    "attempt": expected_attempt,
                    "state": row["state"],
                    "error": row["error"]
                    or {
                        "code": "recovered_after_inactive_job",
                        "message": "已核对原RQ任务不再活动",
                    },
                    "version_ids": list(row["version_ids"]),
                }
            )
        conn.execute(
            text("""
          UPDATE history_runs SET state='queued',attempt=:attempt,job_id=:job,completed_at=NULL,
            error=NULL,attempts=CAST(:history AS jsonb) WHERE id=:id
        """),
            {
                "attempt": attempt,
                "job": "history-" + run_id + "-" + str(attempt),
                "history": canonical(history),
                "id": run_id,
            },
        )
        return _view(
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id"), {"id": run_id}
            )
            .mappings()
            .one()
        )


def run_job_id(run_id):
    with history_engine().begin() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id"), {"id": run_id}
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HistoryError("run_not_found", "任务不存在", 404)
        _source(conn, row["source_id"])
        return row["job_id"]


def _signature_secret():
    secret = os.environ.get("DATAFABRIC_HISTORY_READ_TOKEN", "")
    if not secret:
        raise HistoryError("history_not_configured", "历史读取凭据未配置", 503)
    return secret.encode()


def _cursor(payload):
    raw = canonical(payload).encode()
    packed = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return (
        packed
        + "."
        + hmac.new(_signature_secret(), packed.encode(), hashlib.sha256).hexdigest()
    )


def _decode_cursor(value, binding):
    try:
        packed, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(
            sig,
            hmac.new(_signature_secret(), packed.encode(), hashlib.sha256).hexdigest(),
        ):
            raise ValueError
        payload = json.loads(
            base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4))
        )
        if payload["binding"] != binding:
            raise ValueError
        return payload
    except (ValueError, KeyError, TypeError):
        raise HistoryError(
            "invalid_cursor", "分页游标不匹配来源、范围、模式或截止时间", 422
        ) from None


def _cutoff(value):
    try:
        return utc(value)
    except (ValueError, AttributeError):
        raise HistoryError("invalid_cutoff", "截止时间必须包含时区", 422) from None


def _eligibility(mode):
    if mode in ("current", "operational_replay"):
        return "first_seen_at <= :cutoff"
    if mode == "public_history_reconstruction":
        return "proof_available_at IS NOT NULL AND proof_available_at <= :cutoff"
    raise HistoryError("unsupported_mode", "不支持该历史读取模式")


def _read_filters(
    conn, source_id, is_demo, mode, cutoff, max_seq, run_id, ids, terms, kind
):
    _source(conn, source_id)
    cutoff = _cutoff(cutoff)
    if cutoff > _now(conn):
        raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
    if (
        len(ids) > 5000
        or len(terms) > 3
        or any(not 2 <= len(term) <= 120 for term in terms)
    ):
        raise HistoryError("query_budget", "来源ID或字面检索词超过读取预算")
    where = ["source_id=:source", "is_demo=:demo", "published_seq<=:max_seq"]
    params = {
        "source": source_id,
        "demo": is_demo,
        "cutoff": cutoff,
        "max_seq": max_seq,
    }
    run = None
    if run_id:
        run = (
            conn.execute(
                text("SELECT * FROM history_runs WHERE id=:id"), {"id": run_id}
            )
            .mappings()
            .first()
        )
        if run is None or run["source_id"] != source_id or run["is_demo"] != is_demo:
            raise HistoryError("manifest_mismatch", "采用范围与来源或demo类别不匹配")
        if run["state"] not in ("completed", "partial", "failed"):
            raise HistoryError(
                "manifest_not_ready", "采用任务尚未形成可读取的冻结范围", 409
            )
        # run_id冻结原站ID清单，版本仍在本次max_seq/cutoff下选择。
        # 清单不是过去已知事实；未来才出现的成员单独计not_visible。
        declared_ids = run["request_payload"].get("scope_record_ids") or [
            item["source_record_id"]
            for item in run["request_payload"].get("records", [])
        ]
        if run["kind"] == "fetch":
            declared_ids = (
                conn.execute(
                    text(
                        "SELECT source_record_id FROM history_versions WHERE version_id=ANY(CAST(:ids AS text[]))"
                    ),
                    {"ids": run["version_ids"]},
                )
                .scalars()
                .all()
            )
        where.append("source_record_id=ANY(CAST(:declared_ids AS text[]))")
        params["declared_ids"] = declared_ids
    if ids:
        where.append("source_record_id=ANY(CAST(:native_ids AS text[]))")
        params["native_ids"] = sorted(set(ids))
    if kind:
        where.append("record_kind=:kind")
        params["kind"] = kind
    term_clauses = []
    for index, term in enumerate(sorted(set(terms))):
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term_clauses.append(
            "(payload->>'title' ILIKE :term"
            + str(index)
            + " ESCAPE '\\' OR payload->>'clean_text' ILIKE :term"
            + str(index)
            + " ESCAPE '\\')"
        )
        params["term" + str(index)] = "%" + escaped + "%"
    return " AND ".join(where), params, run, " AND ".join(term_clauses) or "true"


# 同一归档可见点内只用已解析的来源修订日期排先后，不用本地导入顺序冒充源顺序。
_SOURCE_UPDATED_ORDER = """CASE payload->'source_updated_at'->>'precision'
 WHEN 'instant' THEN (payload->'source_updated_at'->>'value')::timestamptz
 WHEN 'day' THEN (((payload->'source_updated_at'->>'value')::date + interval '1 day' - interval '1 microsecond') AT TIME ZONE 'UTC')
 WHEN 'month' THEN ((((payload->'source_updated_at'->>'value') || '-01')::date + interval '1 month' - interval '1 microsecond') AT TIME ZONE 'UTC')
 WHEN 'year' THEN ((((payload->'source_updated_at'->>'value') || '-01-01')::date + interval '1 year' - interval '1 microsecond') AT TIME ZONE 'UTC')
 ELSE NULL END"""


def list_records(
    source_id,
    is_demo,
    mode,
    cutoff,
    *,
    run_id=None,
    source_record_ids=(),
    terms=(),
    record_kind=None,
    limit=25,
    cursor=None,
):
    _derived_mode(source_id, mode)
    if not 1 <= limit <= 100:
        raise HistoryError("query_budget", "每页条数须为1至100")
    binding = sha256(
        canonical(
            {
                "source": source_id,
                "demo": is_demo,
                "mode": mode,
                "cutoff": _cutoff(cutoff).isoformat(),
                "run": run_id,
                "ids": sorted(set(source_record_ids)),
                "terms": sorted(set(terms)),
                "kind": record_kind,
                "limit": limit,
            }
        )
    )
    position = _decode_cursor(cursor, binding) if cursor else None
    with history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        source = _source(conn, source_id)
        max_seq = position["max_seq"] if position else source["published_seq"]
        where, params, run, term_filter = _read_filters(
            conn,
            source_id,
            is_demo,
            mode,
            cutoff,
            max_seq,
            run_id,
            source_record_ids,
            terms,
            record_kind,
        )
        membership_hash = sha256(canonical(params.get("declared_ids", [])))
        if position and position.get("membership_hash") != membership_hash:
            raise HistoryError(
                "manifest_changed",
                "该读取清单已改变，请重新开始读取；旧游标不切换范围",
                409,
            )
        base = "SELECT * FROM history_versions WHERE " + where
        eligible = (
            _eligibility(mode)
            + " AND COALESCE(payload->'facts'->>'publication_role','source_record')!='context_reference'"
        )
        counts = (
            conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE NOT ("
                    + eligible
                    + ")) AS excluded FROM ("
                    + base
                    + ") b"
                ),
                params,
            )
            .mappings()
            .one()
        )
        params.update(last=position["last"] if position else "", limit=limit + 1)
        rows = (
            conn.execute(
                text(
                    """
          WITH eligible AS ("""
                    + base
                    + " AND "
                    + eligible
                    + """),
          ranked AS (SELECT *,row_number() OVER (PARTITION BY source_record_id ORDER BY COALESCE(proof_available_at,first_seen_at) DESC, """
                    + _SOURCE_UPDATED_ORDER
                    + """ DESC NULLS LAST,published_seq DESC) n FROM eligible)
          SELECT payload FROM ranked WHERE n=1 AND source_record_id>:last AND """
                    + term_filter
                    + """ ORDER BY source_record_id LIMIT :limit
        """
                ),
                params,
            )
            .mappings()
            .all()
        )
        items, used_bytes = [], 0
        for row in rows[:limit]:
            record = HistoryRecord.model_validate(row["payload"])
            _check_observation_visibility(conn, record, mode, cutoff)
            record_bytes = len(record.model_dump_json().encode())
            if used_bytes + record_bytes > MAX_RESPONSE_BYTES:
                break
            items.append(record)
            used_bytes += record_bytes
        more = len(rows) > len(items)
        if more and not items:
            raise HistoryError(
                "response_budget", "单条来源记录超过响应预算，请联系维护者", 413
            )
        next_cursor = (
            _cursor(
                {
                    "binding": binding,
                    "max_seq": max_seq,
                    "last": items[-1].source_record_id,
                    "membership_hash": membership_hash,
                }
            )
            if more
            else None
        )
        run_coverage = run["coverage"] if run else {}
        # 对声明包再筛ID/词/类型属于条件子集，不能继承原队列完整性。
        eligible_count = conn.execute(
            text(
                "SELECT count(DISTINCT source_record_id) FROM ("
                + base
                + " AND "
                + eligible
                + ") b"
            ),
            params,
        ).scalar_one()
        declared_count = len(set(params.get("declared_ids", []))) if run else None
        complete = bool(
            run
            and run_coverage.get("complete")
            and not source_record_ids
            and not terms
            and not record_kind
            and eligible_count == declared_count
        )
        coverage = {
            "mode": run_coverage.get("mode", "adopted_records"),
            "scope_description": run["scope_description"]
            if run
            else "本应用已采用版本；不是原站全量",
            "complete": complete,
            "upstream_pagination_exhausted": run_coverage.get(
                "upstream_pagination_exhausted"
            ),
            "as_of": _cutoff(cutoff),
            "run_id": str(run["id"]) if run else None,
            "max_seq": max_seq,
            "declared_count": run_coverage.get("declared_count"),
            "declared_scope_count": declared_count,
            "time_eligible_count": eligible_count,
            "not_visible_in_scope": max(0, declared_count - eligible_count)
            if declared_count is not None
            else 0,
            "adopted_count": run_coverage.get("adopted_count", 0),
            "stop_reason": "page_has_more" if more else "end_of_frozen_selection",
            "limitations": list(run_coverage.get("limitations", []))
            + [
                "一页结束不证明来源全量；候选词/ID筛选不构成完整同期队列",
                "截止资格先筛后选latest；补录和身份补充不倒填首次可见时间",
            ],
        }
        return RecordCollection(
            items=items,
            next_cursor=next_cursor,
            excluded_counts={"ineligible_version_count": counts["excluded"]},
            coverage=coverage,
        )


def read_version(version_id, source_id, source_record_id, is_demo, mode, cutoff):
    _derived_mode(source_id, mode)
    if not re.fullmatch(r"[0-9a-f]{64}", version_id):
        raise HistoryError("version_not_found", "版本不存在", 404)
    with history_engine().begin() as conn:
        _source(conn, source_id)
        cutoff = _cutoff(cutoff)
        if cutoff > _now(conn):
            raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        row = (
            conn.execute(
                text(
                    """
            SELECT payload FROM history_versions WHERE version_id=:id AND source_id=:source
             AND source_record_id=:native AND is_demo=:demo AND """
                    + _eligibility(mode)
                ),
                {
                    "id": version_id,
                    "source": source_id,
                    "native": source_record_id,
                    "demo": is_demo,
                    "cutoff": cutoff,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HistoryError(
                "version_not_found", "该版本在当前许可、范围或截止时间下不可见", 404
            )
        record = HistoryRecord.model_validate(row["payload"])
        _check_observation_visibility(conn, record, mode, cutoff)
        if record.record_kind == "graph":
            _check_graph_document_visibility(conn, record, mode, cutoff)
        return record


def list_changes(source_id, is_demo, *, after=0, limit=25, cursor=None):
    if not 0 <= after or not 1 <= limit <= 100:
        raise HistoryError("query_budget", "变更读取参数不合法")
    binding = sha256(
        canonical(
            {"source": source_id, "demo": is_demo, "after": after, "limit": limit}
        )
    )
    pos = _decode_cursor(cursor, binding) if cursor else None
    with history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        source = _source(conn, source_id)
        max_seq, last = (
            (pos["max_seq"], pos["last"]) if pos else (source["published_seq"], after)
        )
        rows = (
            conn.execute(
                text("""
          SELECT payload FROM history_versions WHERE source_id=:source AND is_demo=:demo
           AND published_seq>:last AND published_seq<=:max_seq ORDER BY published_seq LIMIT :limit
        """),
                {
                    "source": source_id,
                    "demo": is_demo,
                    "last": last,
                    "max_seq": max_seq,
                    "limit": limit + 1,
                },
            )
            .mappings()
            .all()
        )
        items, size = [], 0
        for row in rows[:limit]:
            item = HistoryRecord.model_validate(row["payload"])
            if size + len(item.model_dump_json().encode()) > MAX_RESPONSE_BYTES:
                break
            items.append(item)
            size += len(item.model_dump_json().encode())
        more = len(rows) > len(items)
        if more and not items:
            raise HistoryError("response_budget", "单条记录超过响应预算", 413)
        # 采用序号不是源事实的新旧顺序；仅本页ID返回固定发布边界内的当前头版。
        head_ids = [item.source_record_id for item in items]
        heads = conn.execute(
            text(
                "SELECT DISTINCT ON (source_record_id) source_record_id,version_id "
                "FROM history_versions WHERE source_id=:source AND is_demo=:demo "
                "AND published_seq<=:max_seq AND source_record_id=ANY(:ids) "
                "AND COALESCE(payload->'facts'->>'publication_role','source_record')!='context_reference' "
                "ORDER BY source_record_id,COALESCE(proof_available_at,first_seen_at) DESC,"
                + _SOURCE_UPDATED_ORDER
                + " DESC NULLS LAST,published_seq DESC"
            ),
            {"source": source_id, "demo": is_demo, "max_seq": max_seq, "ids": head_ids},
        ).mappings()
        return {
            "schema_version": "datafabric.history.changes.v1",
            "current_version_ids": {
                **dict.fromkeys(head_ids),
                **{row["source_record_id"]: row["version_id"] for row in heads},
            },
            "items": [item.model_dump(mode="json") for item in items],
            "max_seq": max_seq,
            "next_after": items[-1].published_seq if more else max_seq,
            "next_cursor": _cursor(
                {
                    "binding": binding,
                    "max_seq": max_seq,
                    "last": items[-1].published_seq,
                }
            )
            if more
            else None,
            "scope": "committed_adoptions_not_source_change_feed",
        }


def _check_observation_visibility(conn, record, mode, cutoff):
    if record.parser_version != "datafabric.observation.projection.v0.1":
        return
    from biz_rules.history_sources import parse_json
    from biz_rules.observations import observation

    item = observation(parse_json(record.raw_content))
    for ref in (item.basis.ref, item.supersedes):
        if ref is None:
            continue
        _derived_mode(ref.source_id, mode)
        _source(conn, ref.source_id)
        original = conn.execute(
            text(
                "SELECT payload FROM history_versions WHERE source_id=:source AND source_record_id=:native "
                "AND version_id=:version AND is_demo=:demo AND " + _eligibility(mode)
            ),
            {
                "source": ref.source_id,
                "native": ref.source_record_id,
                "version": ref.version_id,
                "demo": record.is_demo,
                "cutoff": _cutoff(cutoff),
            },
        ).scalar_one_or_none()
        if original is None or original["raw_sha256"] != ref.raw_sha256:
            raise HistoryError(
                "observation_basis_not_visible",
                "观测所引原件在同一许可、模式和截止时间下不可见",
                404,
            )


def _check_graph_document_visibility(conn, record, mode, cutoff):
    from biz_rules.history_sources import parse_json
    from biz_rules.reviewed_graph import GraphBundle

    bundle = GraphBundle.model_validate(parse_json(record.raw_content))
    documents = {}
    for ref in bundle.document_refs:
        _derived_mode(ref.source_id, mode)
        _source(conn, ref.source_id)
        row = conn.execute(
            text(
                "SELECT payload FROM history_versions WHERE version_id=:id AND source_id=:source AND source_record_id=:native AND is_demo=:demo AND "
                + _eligibility(mode)
            ),
            {
                "id": ref.version_id,
                "source": ref.source_id,
                "native": ref.source_record_id,
                "demo": record.is_demo,
                "cutoff": cutoff,
            },
        ).scalar_one_or_none()
        if row is None or row["raw_sha256"] != ref.raw_sha256:
            raise HistoryError(
                "graph_evidence_not_visible",
                "图引用原件在同一许可/模式/截止时间下不可见",
                404,
            )
        documents[
            (
                ref.source_id,
                ref.source_record_id,
                ref.version_id,
                ref.raw_sha256,
                ref.origin_doc_uuid,
            )
        ] = row
    for evidence in [item for node in bundle.nodes for item in node.evidence] + [
        item for edge in bundle.edges for item in edge.evidence
    ]:
        original = documents.get(
            (
                evidence.source_id,
                evidence.source_record_id,
                evidence.version_id,
                evidence.raw_sha256,
                evidence.origin_doc_uuid,
            )
        )
        if (
            original is None
            or original["clean_text"][evidence.locator.start : evidence.locator.end]
            != evidence.quote
            or evidence.locator.end > len(original["clean_text"])
        ):
            raise HistoryError(
                "graph_quote_mismatch", "图证据不属于同截止下可见的冻结原文"
            )

    from biz_rules.native_graph import validate_field_bindings

    validate_field_bindings(bundle, documents, allow_legacy=True)


def read_graph_resolution(
    version_id,
    source_id,
    source_record_id,
    is_demo,
    mode,
    cutoff,
    *,
    name=None,
    external_namespace=None,
    external_id=None,
    limit=20,
    review_scope=None,
):
    from biz_rules.history_sources import parse_json
    from biz_rules.reviewed_graph import GRAPH_SOURCES, GraphBundle, GraphRef, resolve

    if source_id not in GRAPH_SOURCES:
        raise HistoryError("unsupported_graph_source", "请选择固定图来源")
    record = read_version(
        version_id, source_id, source_record_id, is_demo, mode, cutoff
    )
    if record.record_kind != "graph":
        raise HistoryError("not_a_graph_version", "所选版本不是图包")
    return resolve(
        GraphBundle.model_validate(parse_json(record.raw_content)),
        GraphRef(
            source_id=record.source_id,
            source_record_id=record.source_record_id,
            version_id=record.version_id,
            raw_sha256=record.raw_sha256,
            is_demo=record.is_demo,
            first_seen_at=record.first_seen_at,
        ),
        name=name,
        external_namespace=external_namespace,
        external_id=external_id,
        limit=limit,
        review_scope=review_scope,
    )


def _fixed_graph(version_id, source_id, source_record_id, is_demo, mode, cutoff):
    from biz_rules.reviewed_graph import GRAPH_SOURCES, GraphBundle, GraphRef

    if source_id not in GRAPH_SOURCES:
        raise HistoryError("unsupported_graph_source", "请选择固定图来源")
    record = read_version(version_id, source_id, source_record_id, is_demo, mode, cutoff)
    if record.record_kind != "graph":
        raise HistoryError("not_a_graph_version", "所选版本不是图包")
    return GraphBundle.model_validate_json(record.raw_content), GraphRef(
        source_id=record.source_id, source_record_id=record.source_record_id,
        version_id=record.version_id, raw_sha256=record.raw_sha256,
        is_demo=record.is_demo, first_seen_at=record.first_seen_at,
    )


def read_graph_paths(version_id, source_id, source_record_id, is_demo, mode, cutoff, *,
                     start, target, relation_types=(), direction="out",
                     review_scope="reviewed", max_depth=4, max_paths=10, max_expansions=1000):
    from biz_rules.graph_tools import graph_identity, paths

    bundle, ref = _fixed_graph(version_id, source_id, source_record_id, is_demo, mode, cutoff)
    return paths(bundle, ref, graph_identity(start), graph_identity(target),
                 relation_types=relation_types, direction=direction, review_scope=review_scope,
                 max_depth=max_depth, max_paths=max_paths, max_expansions=max_expansions)


def read_graph_subgraph(version_id, source_id, source_record_id, is_demo, mode, cutoff, *,
                        node_ids, relation_types=(), review_scope="reviewed",
                        max_nodes=100, max_edges=200):
    from biz_rules.graph_tools import graph_identity, subgraph

    bundle, ref = _fixed_graph(version_id, source_id, source_record_id, is_demo, mode, cutoff)
    return subgraph(bundle, ref, [graph_identity(value) for value in node_ids],
                    relation_types=relation_types, review_scope=review_scope,
                    max_nodes=max_nodes, max_edges=max_edges)
