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

from clean import registry_changes
from fetch.sources import project_record, stable_raw
from fetch.window_coverage import MAX_RUNS, effective_receipts
from store.history_models import (
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
                # a publisher excludes other publishers of the source only: it changes published_seq and catalog,
                # never the key, so it must not wait on (or upgrade past) the FOR KEY SHARE locks that inserting a
                # run or a version of the source takes through the foreign key - FOR UPDATE would, and two imports
                # that both inserted their run before publishing deadlock
                + (" FOR NO KEY UPDATE" if lock else "")
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


def sources_receipt():
    """GET /api/history/sources (store.source_listing): the readable sources and the retired ones, each with its
    name and the capabilities the radar reads."""
    from store import source_listing

    with history_engine().begin() as conn:
        rows = list(conn.execute(text(
            "SELECT source_id, read_enabled, catalog FROM history_sources ORDER BY source_id")).mappings())
    return source_listing.listing(rows, allowed_sources(), _catalog_dependencies)


def list_sources():
    """The readable sources that may be offered (retired ones are in sources_receipt()['retired_sources'])."""
    return sources_receipt()["sources"]


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


def _evidence_text(conn, row, locator):
    """证据定位所指的那份正文：投影正文，或该版本按处理器版本+哈希指认的派生清洗正文。"""
    if locator.basis != "derived_clean_text":
        return row["clean_text"]
    derived = conn.execute(text(
        "SELECT text, text_sha256 FROM derived_clean_text WHERE version_id=:v AND processor_version=:pv "
        "ORDER BY attempt DESC LIMIT 1"), {"v": row["version_id"], "pv": locator.text_processor_version}
    ).mappings().first()
    if derived is None or derived["text_sha256"] != locator.text_sha256:
        raise HistoryError("graph_quote_mismatch", "图证据指认的派生清洗正文不存在或已不一致")
    return derived["text"] or ""


def _ref_key(ref):
    return (
        ref.source_id,
        ref.source_record_id,
        ref.version_id,
        ref.raw_sha256,
        ref.origin_doc_uuid,
    )


def _refuse_retired_refs(refs):
    """A package published now cites the real source version a legacy one became (legacy_aliases.real_version_id),
    never the retired bridge. Packages published before keep validating on read: this is the publish path only."""
    from fetch.retired_sources import RETIRED

    retired = sorted({ref.source_id for ref in refs if ref.source_id in RETIRED})
    if retired:
        raise HistoryError(
            "graph_evidence_retired_source",
            "新图包不能引用已退役来源 " + ",".join(retired) + "；请引用迁入后的真实来源版本（legacy_aliases.real_version_id）")


def _graph_documents(conn, run, bundle, now):
    """The frozen originals a graph bundle cites (by reference key) and the sources they depend on."""
    documents, dependencies = {}, set()
    _refuse_retired_refs(bundle.document_refs)
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
        key = _ref_key(ref)
        if key in documents:
            raise HistoryError("duplicate_graph_document", "图包文档清单重复")
        documents[key] = row
    return documents, dependencies


def _check_graph_times(bundle, now):
    for node in bundle.nodes:
        if node.review.reviewed_at is not None and node.review.reviewed_at > now:
            raise HistoryError("future_graph_review", "源审核时间晚于本次服务器采用")
    for edge in bundle.edges:
        if edge.review.reviewed_at is not None and edge.review.reviewed_at > now:
            raise HistoryError("future_graph_review", "源审核时间晚于本次服务器采用")


def _check_graph_quotes(conn, bundle, documents):
    for ref in [value for node in bundle.nodes for value in node.evidence] + [
        value for edge in bundle.edges for value in edge.evidence
    ]:
        original = documents.get(_ref_key(ref))
        if original is None:
            raise HistoryError(
                "graph_evidence_outside_manifest", "图证据不属于本包声明的文档版本"
            )
        locator = ref.locator
        body = _evidence_text(conn, original, locator)
        if body[locator.start : locator.end] != ref.quote or locator.end > len(body):
            raise HistoryError(
                "graph_quote_mismatch", "图证据与冻结原文字符范围不一致，不能标记已绑定"
            )


def _validate_graph_bundle(conn, run, bundle, now):
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
    documents, dependencies = _graph_documents(conn, run, bundle, now)
    if dependencies != set(bundle.dependency_source_ids):
        raise HistoryError(
            "graph_source_dependencies_mismatch",
            "图包依赖必须等于服务器核对的原件及原站许可",
        )
    _check_graph_times(bundle, now)
    _check_graph_quotes(conn, bundle, documents)
    if any(
        item.merged_at is not None and item.merged_at > now for item in bundle.redirects
    ):
        raise HistoryError("future_graph_merge", "源身份合并时间晚于本次服务器采用")
    from graph.publish.native_graph import validate_field_bindings

    validate_field_bindings(bundle, documents)
    return dependencies


def _validate_derived(conn, run, item, now, proof):
    from fetch.sources import parse_json
    from graph.publish.reviewed_graph import DERIVED_SOURCES, GraphBundle, StoredDocument

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
            from graph.publish.collected_document import CollectedDocument

            original = CollectedDocument.model_validate(data)
        else:
            original = StoredDocument.model_validate(data)
        _source(conn, original.source_site)
        return {original.source_site}

    return _validate_graph_bundle(conn, run, GraphBundle.model_validate(data), now)


def _validate_observation(conn, run, data, now):
    from fetch.observations import (
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
    from fetch.sources import parse_json
    from graph.publish.reviewed_graph import (
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
    values = {key: row[key] for key in RunView.model_fields if key in row}
    if row["kind"] == "fetch":
        request = row["request_payload"]
        values["fetch_mode"] = "simulated" if row["is_demo"] else "public"
        values["demo_instance_id"] = request.get("demo_instance_id")
        values["input_sha256"] = (request.get("_demo_fetch") or {}).get("input_sha256")
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


def _current_version(conn, run, item):
    """The record's current version by Fabric's own rule (context references excluded; latest proof or
    first sight, then the source's own update time)."""
    return conn.execute(
        text(
            "SELECT version_id,payload FROM " + _VERSIONS + " "
            "WHERE source_id=:source AND is_demo=:demo AND source_record_id=:native "
            "AND " + _ROLE + "!='context_reference' "
            "ORDER BY " + _LATEST + " LIMIT 1"
        ),
        {"source": run["source_id"], "demo": run["is_demo"], "native": item.source_record_id},
    ).mappings().first()


# Stored with each new version but not part of its identity: they are computed from the original and the projection,
# so they cannot tell two versions apart, and versions stored before they existed keep matching their originals.
DERIVED_PAYLOAD_FIELDS = frozenset({"document_evidence"})


def _same_identity(payload, identity, *, ignore_raw=False):
    """Whether a stored version has this identity (review clocks never count; the raw hash only when
    asked: parser version, media type, projection, proof and mapping always do)."""
    prior = {key: payload.get(key) for key in identity}
    prior["availability_proof"] = {
        key: value for key, value in (prior["availability_proof"] or {}).items() if key != "verified_at"
    }
    if prior["mapping_review"]:
        prior["mapping_review"] = {
            key: value for key, value in prior["mapping_review"].items() if key != "reviewed_at"
        }
    mine = dict(identity)
    if ignore_raw:
        prior.pop("raw_sha256")
        mine.pop("raw_sha256")
        for side in (prior, mine):  # the hash of a web capture's HTML is a fact about the raw (stable_raw)
            side["facts"] = {k: v for k, v in (side["facts"] or {}).items() if k != "html_sha256"}
    return canonical(prior) == canonical(mine)

def import_archive(request: ArchiveImport, key: str):
    # 全包先验证再发布；任何一条非法均不部分提交或改变已有成果。
    if request.source_id == "legacy_documents" and legacy_retired():
        raise HistoryError(*LEGACY_RETIRED, 410)  # v3 6.3：旧桥不再新写，没有开关
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
    """Adopt the prepared originals as versions of the run's source (one transaction) and close the run."""
    source = _source(conn, run["source_id"], lock=True)
    seq = source["published_seq"]
    version_ids, new_ids = [], []
    dependencies = set()
    for item, projected in prepared:
        dependencies.update(_validate_derived(conn, run, item, now, proof))
        mapping_review, mapping_time = None, None
        if item.source_record_id in verified_mapping_ids:
            mapping_review, mapping_time = _mapping_review(projected, proof, now, mapping_basis)
        if proof.kind == "reviewed_archive":
            _check_archive_bounds(projected, proof, mapping_time if mapping_review else None)
            _check_archive_order(conn, run, item, projected, proof)
        identity = _version_identity(run, item, projected, proof, mapping_review)
        version_id, existing, current = _adopted_version(conn, run, item, identity)
        if existing is None:
            seq += 1
            _insert_version(conn, run, item, projected, proof, mapping_review, version_id, seq, now, current)
            new_ids.append(version_id)
        version_ids.append(version_id)
    _persist_dependencies(conn, source, dependencies)
    _link_attachments(conn, run, prepared, version_ids)
    conn.execute(
        text("UPDATE history_sources SET published_seq=:seq WHERE source_id=:id"),
        {"seq": seq, "id": run["source_id"]},
    )
    _close_run(conn, run, prepared, version_ids, new_ids, coverage, error, now)


_MAPPING_FIELDS = ("program_id", "lineage_id", "indication", "jurisdiction", "mapping_available_at")


def _mapping_review(projected, proof, now, basis):
    """A reviewed program mapping needs its five sourced fields and a public time no later than this adoption."""
    facts = projected["facts"]
    if not all(isinstance(facts.get(key), str) and facts[key].strip() for key in _MAPPING_FIELDS):
        raise HistoryError("mapping_incomplete", "审核项目映射缺少五项有出处字段")
    mapping_time = source_time(facts["mapping_available_at"])
    if mapping_time is None or mapping_time.upper_bound() > now:
        raise HistoryError("invalid_mapping_time", "映射公开时间格式错误或晚于本次采用")
    review = MappingReview(reviewed_by="history-admin", reviewed_at=now, basis=basis, available_at=proof.available_at)
    return review, mapping_time


def _check_archive_bounds(projected, proof, mapping_time):
    """后续结局/新映射不能借旧原件证明倒灌到历史特征。"""
    bounds = [projected.get("source_published_at"), projected.get("source_updated_at")]
    if projected["record_kind"] == "outcome":
        facts = projected["facts"]
        bounds += [source_time(facts.get(key)) for key in ("event_at", "public_available_at", "observation_end")]
    if mapping_time is not None:
        bounds.append(mapping_time)
    for bound in bounds:
        value = TimeValue.model_validate(bound) if isinstance(bound, dict) else bound
        if value and value.upper_bound() > proof.available_at.upper_bound():
            raise HistoryError("archive_time_conflict", "原件新增事实、结局窗口或映射晚于归档可见证明")


def _check_archive_order(conn, run, item, projected, proof):
    """Another archived original of the same record whose proof window overlaps this one must be ordered by an
    exact proof time and distinguishable source update times; otherwise the revision order is unknown."""
    prior_versions = conn.execute(
        text("""
          SELECT payload FROM history_versions
          WHERE source_id=:source AND is_demo=:demo AND source_record_id=:native
            AND proof_available_at IS NOT NULL
        """),
        {"source": run["source_id"], "demo": run["is_demo"], "native": item.source_record_id},
    ).scalars()
    updated = TimeValue.model_validate(projected["source_updated_at"]) if projected["source_updated_at"] else None
    for previous in prior_versions:
        if previous["raw_sha256"] == sha256(item.raw_content):
            continue
        old_proof = TimeValue.model_validate(previous["availability_proof"]["available_at"])
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
            TimeValue.model_validate(previous["source_updated_at"]) if previous.get("source_updated_at") else None
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


def _version_identity(run, item, projected, proof, mapping_review):
    # 本次审核/采用时钟不参与版本身份；重复提交同原件/同审核依据复用原版本。
    identity_proof = {k: v for k, v in proof.model_dump(mode="json").items() if k != "verified_at"}
    identity_mapping = mapping_review.model_dump(mode="json") if mapping_review else None
    if identity_mapping:
        identity_mapping.pop("reviewed_at")
    return {
        "source_id": run["source_id"],
        "source_record_id": item.source_record_id,
        "is_demo": run["is_demo"],
        "raw_sha256": sha256(item.raw_content),
        "raw_media_type": item.raw_media_type,
        "parser_version": PARSER_VERSION,
        **{key: value for key, value in projected.items() if key not in DERIVED_PAYLOAD_FIELDS},
        "availability_proof": identity_proof,
        "mapping_review": identity_mapping,
    }


def _adopted_version(conn, run, item, identity):
    """(version_id, existing row or None, current version of the record for a fetch or None)."""
    version_id = sha256(canonical(identity))
    existing = conn.execute(
        text("SELECT version_id FROM history_versions WHERE version_id=:id"),
        {"id": version_id},
    ).first()
    current = _current_version(conn, run, item) if run["kind"] == "fetch" else None
    stable = stable_raw(run["source_id"], item.raw_content) if current is not None else None
    if (
        stable is not None
        and stable_raw(run["source_id"], current["payload"].get("raw_content")) == stable
        and _same_identity(current["payload"], identity, ignore_raw=True)
    ):
        # 公开观察只差JSON序列化（键顺序、空白）、来源每次改写的数据日期（ClinicalTrials versionHolder），
        # 或网页原件里的 HTML（解析结果和附件清单没变）：仍是当前版本；原件、raw_sha256、first_seen 都保留
        # 首次所见。取舍：网页只有解析结果或附件清单变了才出新版本，HTML 里解析没读到的变动不记。
        # 不看 existing：A→A'（只 HTML 不同）→A 也始终是同一个版本。档案导入不走这里。
        version_id, existing = current["version_id"], (current["version_id"],)
    elif run["kind"] == "fetch" and existing is not None and current is not None:
        # 公开观察只把“与当前相同”当重复；A→B→A是一次新的采用。
        # 档案导入仍按原件/证明去重，不把迟到的旧档案改成当前状态。
        if _same_identity(current["payload"], identity):
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
    return version_id, existing, current


def stored_unchanged(source_id, item) -> bool:
    """存下这条原件会不会多出新版本？不会（库里这条记录的当前版本就是它）= True。增量读取（FetchRequest.known_stop）
    用它判断“读到旧的了”。直接走 _adopted_version，和 _publish 判断“新版本还是没变”的是同一段代码，不会两套规则。
    原件投影失败时返回 False：不在这里报错，留给存版本时按原样报。"""
    run = {"source_id": source_id, "is_demo": False, "kind": "fetch"}
    try:
        projected = project_record(source_id, item)
    except HistoryError:
        return False
    identity = _version_identity(run, item, projected, AvailabilityProof(kind="unverified"), None)
    with history_engine().begin() as conn:
        return _adopted_version(conn, run, item, identity)[1] is not None


def _insert_version(conn, run, item, projected, proof, mapping_review, version_id, seq, now, current):
    payload = HistoryRecord(
        source_id=run["source_id"],
        source_record_id=item.source_record_id,
        version_id=version_id,
        is_demo=run["is_demo"],
        raw_content=item.raw_content,
        raw_media_type=item.raw_media_type,
        raw_sha256=sha256(item.raw_content),
        first_seen_at=now,
        received_at=now,
        availability_proof=proof,
        mapping_review=mapping_review,
        published_seq=seq,
        **projected,
    )
    proof_at = proof.available_at.upper_bound() if proof.kind == "reviewed_archive" else None
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
    conn.execute(
        text(
            _DERIVE + " FROM history_versions h WHERE h.version_id=:id AND NOT EXISTS "
            "(SELECT 1 FROM history_version_rank r WHERE r.version_id=h.version_id)"
        ),
        {"id": version_id},
    )
    if run["kind"] == "fetch":
        # 确定性加工和版本同一事务：版本可见时，它与上一版的登记差分也已就绪
        registry_changes.record(
            conn,
            source_id=run["source_id"],
            is_demo=run["is_demo"],
            record_id=item.source_record_id,
            previous=current,
            to_version_id=version_id,
            facts=projected.get("facts"),
            locators=projected.get("field_locators"),
            pairing="current_at_adoption",
            summary=projected.get("clean_text"),
            raw=item.raw_content,
        )


# 因为条数、页数或截止时间停下的读取，来源没读完：状态是 partial，不是 completed
_LIMIT_STOPS = ("page_budget", "record_budget", "deadline")


def _close_run(conn, run, prepared, version_ids, new_ids, coverage, error, now):
    state = "partial" if error and version_ids else "failed" if error else "completed"
    if run["kind"] == "fetch" and not error and coverage.get("stop_reason") in _LIMIT_STOPS:
        state = "partial"
    coverage = {
        **coverage,
        "new_version_count": len(new_ids),
        "same_version_count": len(version_ids) - len(new_ids),
        "adopted_count": len(version_ids),
    }
    if "backfill" in coverage:
        # Only what this run created is background: a version an earlier run already adopted was first seen then.
        coverage["backfill"] = {**coverage["backfill"], "background_version_ids": new_ids}
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


def _link_attachments(conn, run, prepared, version_ids):
    """附件版本 → 固定父版本（attachment_of）。父页面在同一次读取里就记下它这次的版本；不在（例如按旧库
    链接补抓附件、父页面没有重抓）就如实记父版本未知，不猜。同一组关系只记一次。"""
    by_record = {item.source_record_id: version for (item, _), version in zip(prepared, version_ids)}
    for (item, projected), version in zip(prepared, version_ids):
        facts = projected.get("facts") or {}
        if facts.get("role") != "attachment" or not facts.get("parent_record_id"):
            continue
        parent = facts["parent_record_id"]
        parent_version = by_record.get(parent)
        conn.execute(
            text("""
              INSERT INTO history_attachment_links
                (attachment_version_id,parent_record_id,parent_version_id,link_basis,run_id)
              SELECT :a,:p,:pv,:basis,:run
              WHERE NOT EXISTS (SELECT 1 FROM history_attachment_links WHERE attachment_version_id=:a
                                AND parent_record_id=:p AND COALESCE(parent_version_id,'')=COALESCE(:pv,''))
            """),
            {
                "a": version,
                "p": parent,
                "pv": parent_version,
                "basis": "same_run_parent_version" if parent_version else "parent_version_unknown",
                "run": run["id"],
            },
        )


def create_fetch(request: FetchRequest, key: str):
    from fetch.fetch_job import freeze_demo_fetch, require_fetch_allowed

    require_fetch_allowed(request)
    body = request.model_dump(mode="json", exclude_none=True)
    # Preserve hashes of pre-demo public requests and their existing idempotency keys.
    if not request.is_demo:
        body.pop("is_demo")
    if not request.known_stop:  # likewise for a read that is not an incremental one
        body.pop("known_stop")
    with history_engine().begin() as conn:
        stored = None
        if request.is_demo:
            existing = conn.execute(
                text("SELECT id FROM history_runs WHERE idempotency_key=:key"), {"key": key}
            ).first()
            if existing is None:
                stored = {**body, "_demo_fetch": freeze_demo_fetch(request)}
        row, fresh = _new_run(
            conn, request.source_id, request.is_demo, "fetch", body, key,
            ("隔离模拟补采：" if request.is_demo else "公开有界检索：") + request.query,
            stored_body=stored,
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
        from fetch.fetch_job import require_fetch_allowed

        body = dict(row["request_payload"])
        frozen = body.pop("_demo_fetch", None)
        request = FetchRequest.model_validate(body)
        require_fetch_allowed(request)
        if request.is_demo != row["is_demo"]:
            raise HistoryError("fetch_identity_mismatch", "补采任务与模拟身份不匹配", 409)
        conn.execute(
            text("UPDATE history_runs SET state='running' WHERE id=:id"), {"id": run_id}
        )
        return request, frozen


def _reported_coverage(batch):
    """The parts of a read's coverage that only some plugins report: where to resume, how each attachment went,
    which items could not be read (url and reason), for a windowed read what each window returned and which
    intervals may be cut short, and for a read made by an explicit backfill its receipt (ops.who_backfill),
    which marks the run's new versions as background."""
    reported = {}
    if batch.get("next_continuation") is not None:
        reported["next_continuation"] = batch["next_continuation"]
    if batch.get("attachment_coverage"):
        # 本次读取每个附件读没读到（站点插件），留在读取记录里，不进原件
        reported["attachment_coverage"] = batch["attachment_coverage"][:500]
    if batch.get("unread_items"):
        # 本次读取里读不到的条目（站点插件）：没存版本，只在这里留一份
        reported["unread_items"] = batch["unread_items"][:500]
    if batch.get("skipped_items"):
        # 来源列了、但没有内容可存的行（openFDA 待发布的信函）：不是读不到，不影响“读完”，留一份说明
        reported["skipped_items"] = batch["skipped_items"][:500]
    for key in ("window_coverage", "truncated_intervals", "backfill"):
        if key in batch:
            reported[key] = batch[key]
    return reported


def background_version_ids(conn) -> list[str]:
    """The versions an explicit backfill created: the receipts in the coverage of its runs (ops.who_backfill) list
    them as background. Material that is not new activity; a version an earlier run had adopted is not in them."""
    return [r[0] for r in conn.execute(text(
        "SELECT DISTINCT jsonb_array_elements_text(coverage->'backfill'->'background_version_ids') FROM history_runs "
        "WHERE jsonb_typeof(coverage->'backfill'->'background_version_ids') = 'array'"))]


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
        from fetch.fetch_job import read_demo_fetch, require_fetch_allowed

        body = dict(run["request_payload"])
        frozen = body.pop("_demo_fetch", None)
        request = FetchRequest.model_validate(body)
        require_fetch_allowed(request)
        if request.is_demo != run["is_demo"]:
            raise HistoryError("fetch_identity_mismatch", "补采任务与模拟身份不匹配", 409)
        if request.is_demo:
            expected = read_demo_fetch(request, frozen)
            if (batch.get("demo_instance_id") != request.demo_instance_id
                    or batch.get("input_sha256") != expected["input_sha256"]
                    or [item.model_dump(mode="json") for item in batch["records"]]
                       != [item.model_dump(mode="json") for item in expected["records"]]):
                raise HistoryError("demo_input_invalid", "模拟补采结果与冻结原件不匹配", 422)
            batch = expected
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
        coverage.update(_reported_coverage(batch))
        if request.is_demo:
            coverage.update(
                is_demo=True, fetch_mode="simulated", demo_instance_id=request.demo_instance_id,
                input_sha256=batch["input_sha256"],
            )
        coverage.update(
            mode="bounded_demo_query" if request.is_demo else "bounded_public_query",
            limitations=[
                "隔离模拟包的字面查询与有界采用；不是外部公开采集或完整领域队列"
                if request.is_demo else
                "有界公开检索不是完整历史队列；不据此训练成功项目筛选后的伪队列"
            ],
        )
        if batch.get("count_drift"):
            coverage["count_drift"] = batch["count_drift"]
            coverage["limitations"].append(
                "来源命中数在翻页中途下降；按上游结束标记收尾，已读数少于最初命中数"
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
    from fetch.fetch_job import require_retry_allowed

    require_retry_allowed()
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
        from fetch.fetch_job import require_fetch_allowed

        require_fetch_allowed(_view(row))
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


# 整源字面检索（ILIKE 扫正文）只在不大的来源上做；超过这个版本数立即返回 query_budget，引导用 WeKnora 检索
# （POST /api/history/search）。给了记录编号或采用范围的读取不受影响。
LITERAL_SEARCH_MAX_VERSIONS = 20000


def _read_filters(
    conn, source_id, is_demo, mode, cutoff, max_seq, run_id, ids, terms, kind
):
    source = _source(conn, source_id)
    if terms and not ids and not run_id and source["published_seq"] > LITERAL_SEARCH_MAX_VERSIONS:
        raise HistoryError(
            "query_budget",
            "该来源有 " + str(source["published_seq"]) + " 个版本，整源字面检索超出读取预算；"
            "请用 POST /api/history/search（WeKnora 检索），或给出记录编号、采用范围",
            422,
        )
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
_SOURCE_UPDATED_FROM_PAYLOAD = """CASE payload->'source_updated_at'->>'precision'
 WHEN 'instant' THEN (payload->'source_updated_at'->>'value')::timestamptz
 WHEN 'day' THEN (((payload->'source_updated_at'->>'value')::date + interval '1 day' - interval '1 microsecond') AT TIME ZONE 'UTC')
 WHEN 'month' THEN ((((payload->'source_updated_at'->>'value') || '-01')::date + interval '1 month' - interval '1 microsecond') AT TIME ZONE 'UTC')
 WHEN 'year' THEN ((((payload->'source_updated_at'->>'value') || '-01-01')::date + interval '1 year' - interval '1 microsecond') AT TIME ZONE 'UTC')
 ELSE NULL END"""
_ROLE_FROM_PAYLOAD = "COALESCE(payload->'facts'->>'publication_role','source_record')"
# V021: ranking reads the derived keys; a version not derived yet (no rank row, publication_role NULL) is
# ranked from its payload, the same values, so a partial backfill changes no result.
_VERSIONS = "history_versions LEFT JOIN history_version_rank USING (version_id)"
_ROLE = "COALESCE(publication_role," + _ROLE_FROM_PAYLOAD + ")"
_SOURCE_UPDATED_ORDER = (
    "(CASE WHEN publication_role IS NULL THEN " + _SOURCE_UPDATED_FROM_PAYLOAD + " ELSE source_updated_rank END)"
)
# 一条记录的“最新版本”：原生版本先于 v3 6.3 迁入的旧版本（旧内容晚迁入不抢原生现版，也不让原生的撤回/
# 更正“复活”）；同类之间看最晚的证明或首次可见时间，再看来源自己的更新时间，最后看发布序号。
# 来源更新时间只在前一项相同时才排先后，不表示“来源改过”；它是什么时间看该字段自带的 meaning（GDELT 的是观察时间）。
# 当前头版、截止时间下可见的版本（先按模式和截止时间筛，再排序）、变更列表的头版都用这一条规则。
_LATEST = (
    "(" + _ROLE + "='migrated_record'),"
    "COALESCE(proof_available_at,first_seen_at) DESC," + _SOURCE_UPDATED_ORDER + " DESC NULLS LAST,published_seq DESC"
)


def _ranked(where, mode):
    """截止资格先筛（不含 context_reference），再按 _LATEST 给每条记录的版本排序；ranked 里 n=1 是此刻可见的版本。
    list_records 与 visible_versions 共用这一段，可见性只有一套规则。"""
    return (
        "WITH eligible AS (SELECT * FROM " + _VERSIONS + " WHERE " + where + " AND " + _eligibility(mode)
        + " AND " + _ROLE + "!='context_reference'), ranked AS (SELECT *,row_number() OVER "
        "(PARTITION BY source_record_id ORDER BY " + _LATEST + ") n FROM eligible) "
    )
_DERIVE = (
    "INSERT INTO history_version_rank(version_id,publication_role,source_updated_rank) SELECT version_id,"
    + _ROLE_FROM_PAYLOAD
    + ","
    + _SOURCE_UPDATED_FROM_PAYLOAD
)


def backfill_rank(source_id, *, batch=500):
    """Derive the ranking keys of up to `batch` versions of one source written before V021; returns how
    many were added (0 when done). Idempotent; reads each payload once; history_versions is not touched."""
    with history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '30000ms'"))
        return conn.execute(
            text(
                _DERIVE + " FROM history_versions WHERE version_id IN (SELECT h.version_id FROM history_versions h "
                "LEFT JOIN history_version_rank r ON r.version_id=h.version_id "
                "WHERE h.source_id=:source AND r.version_id IS NULL LIMIT :batch)"
            ),
            {"source": source_id, "batch": batch},
        ).rowcount


def legacy_retired() -> bool:
    """v3 6.3: the legacy_documents bridge is retired - nothing new is written to it (documents come in through
    their real sources, collected or migrated) and no new query chooses it; versions already there stay readable
    by their old ids (reports, graphs, audit, aliases). There is no environment switch that reopens it. The function
    is the one place the old writer's code paths ask, kept for the contract tests of the old writer, which replace it
    explicitly; the service never does."""
    return True


LEGACY_RETIRED = ("legacy_retired", "旧文档冻结桥已退役（v3 6.3）：不再写入、不再作新查询的来源；文档经各真实来源进入，"
                  "旧版本按旧编号仍可读")


def _list_binding(source_id, is_demo, mode, cutoff, run_id, source_record_ids, terms, record_kind, limit, window):
    """Cursor binding: a cursor is only valid for the exact read it was issued for."""
    binding = {
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
    if window:  # a read without a window keeps the binding it always had
        binding["window"] = [moment.isoformat() for moment in window]
    return sha256(canonical(binding))


# A lead listing reads a source's records by the time its source_updated_at holds. For GDELT that is the time GDELT
# first observed the article (meaning gdelt_first_observed), the very field its read windows are cut by.
LEAD_WINDOW_SOURCES = ("gdelt_doc",)
WINDOW_LIMITATION = ["窗口回执只记本系统对该窗口的读取：条数少于上限只说明没碰到上限，不是来源在该窗口的全量；"
                     "possibly_truncated、failed、not_read 的区间可能漏了线索"]


def _lead_window(source_id, window):
    """The (start, end) UTC range of a lead listing, or None when no window is asked for."""
    if window == (None, None):
        return None
    try:
        bounds = (utc(window[0]), utc(window[1]))
    except (ValueError, AttributeError):
        bounds = None
    if source_id not in LEAD_WINDOW_SOURCES or bounds is None or bounds[0] >= bounds[1]:
        raise HistoryError("invalid_window", "线索窗口须给出含时区的起止时间（起点早于终点），且只适用于 GDELT 新闻索引", 422)
    return bounds


def _in_window(where, params, window):
    """`where` narrowed to the versions whose source_updated_at lies in [start, end)."""
    if window is None:
        return where
    params.update(window_start=window[0], window_end=window[1])
    return where + " AND " + _SOURCE_UPDATED_ORDER + ">=:window_start AND " + _SOURCE_UPDATED_ORDER + "<:window_end"


def _window_receipts(conn, source_id, is_demo, run, cutoff, window):
    """(receipts, truncated intervals) a listing carries: those of the named run, or - for a window without a run -
    those of the source's finished fetch runs that ended by the cutoff and read a range that meets the window."""
    if run is not None:
        runs = [(run["request_payload"].get("query"), run["coverage"])]
    elif window is None:
        return [], []
    else:
        rows = conn.execute(text(
            "SELECT request_payload->>'query' AS query,coverage FROM history_runs "
            "WHERE source_id=:source AND is_demo=:demo AND kind='fetch' AND state IN ('completed','partial','failed') "
            "AND completed_at<=:cutoff AND jsonb_typeof(coverage->'window_coverage')='array' "
            "AND COALESCE((request_payload->>'updated_until')::timestamptz,completed_at)>:start "
            "AND COALESCE((request_payload->>'updated_since')::timestamptz,completed_at-interval '24 hours')<:end "
            "ORDER BY completed_at,id LIMIT :limit"
        ), {"source": source_id, "demo": is_demo, "cutoff": _cutoff(cutoff), "start": window[0], "end": window[1],
            "limit": MAX_RUNS + 1}).mappings().all()
        if len(rows) > MAX_RUNS:
            raise HistoryError("window_too_wide", "窗口内的读取运行超过 %d 次，请缩小窗口" % MAX_RUNS, 422)
        runs = [(row["query"], row["coverage"]) for row in rows]
    return effective_receipts(runs, window)


def _take_page(conn, rows, limit, mode, cutoff):
    """The records of one page within the response byte budget, and whether more follow."""
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
    return items, more


def _list_coverage(run, params, eligible_count, max_seq, cutoff, more, filtered, windows):
    """What the page does and does not prove about the source; `filtered` = ids/terms/kind narrowed the read."""
    run_coverage = run["coverage"] if run else {}
    declared_count = len(set(params.get("declared_ids", []))) if run else None
    # 对声明包再筛ID/词/类型属于条件子集，不能继承原队列完整性。
    complete = bool(
        run
        and run_coverage.get("complete")
        and not filtered
        and eligible_count == declared_count
    )
    return {
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
        ]
        + (WINDOW_LIMITATION if windows[0] else []),
        "window_coverage": windows[0],
        "truncated_intervals": windows[1],
    }


def _count_ineligible(conn, base, eligible, params):
    return (
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
        .one()["excluded"]
    )


def _page_rows(conn, where, mode, term_filter, params):
    return (
        conn.execute(
            text(
                _ranked(where, mode)
                + """
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


def _count_eligible_records(conn, base, eligible, params):
    return conn.execute(
        text(
            "SELECT count(DISTINCT source_record_id) FROM ("
            + base
            + " AND "
            + eligible
            + ") b"
        ),
        params,
    ).scalar_one()


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
    window=(None, None),
):
    if source_id == "legacy_documents" and legacy_retired() and not source_record_ids:
        raise HistoryError(*LEGACY_RETIRED, 410)  # 按旧编号（source_record_ids）的读取不受影响
    _derived_mode(source_id, mode)
    if not 1 <= limit <= 100:
        raise HistoryError("query_budget", "每页条数须为1至100")
    window = _lead_window(source_id, window)
    binding = _list_binding(source_id, is_demo, mode, cutoff, run_id, source_record_ids, terms, record_kind, limit, window)
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
        where = _in_window(where, params, window)
        membership_hash = sha256(canonical(params.get("declared_ids", [])))
        if position and position.get("membership_hash") != membership_hash:
            raise HistoryError(
                "manifest_changed",
                "该读取清单已改变，请重新开始读取；旧游标不切换范围",
                409,
            )
        base = "SELECT * FROM " + _VERSIONS + " WHERE " + where
        eligible = (
            _eligibility(mode)
            + " AND "
            + _ROLE
            + "!='context_reference'"
        )
        excluded = _count_ineligible(conn, base, eligible, params)
        params.update(last=position["last"] if position else "", limit=limit + 1)
        rows = _page_rows(conn, where, mode, term_filter, params)
        items, more = _take_page(conn, rows, limit, mode, cutoff)
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
        eligible_count = _count_eligible_records(conn, base, eligible, params)
        filtered = bool(source_record_ids or terms or record_kind or window)
        windows = _window_receipts(conn, source_id, is_demo, run, cutoff, window)
        return RecordCollection(
            items=items,
            next_cursor=next_cursor,
            excluded_counts={"ineligible_version_count": excluded},
            coverage=_list_coverage(run, params, eligible_count, max_seq, cutoff, more, filtered, windows),
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
        if source_id == "legacy_documents":
            record = _with_legacy_alias(conn, record, mode, cutoff)
        return record


def _with_legacy_alias(conn, record, mode, cutoff):
    """v3 6.3: a legacy version read by its old id stays exactly that version (content, locators, times) and
    says what it became. Migrated: the real source must be readable now, or the old id is blocked too (no way
    round a revoked source); the alias then reports the real version's visibility at this mode and cutoff and
    the real record's current version there (withdrawn or not). Pending: its reason; no access is widened."""
    from fetch.legacy_migration import alias

    # the bridge's permission is the intersection of its original sites: a revoked site stops its old versions whether
    # or not they were migrated (the bridge row's own dependency list may be empty)
    site = (record.facts or {}).get("source_site")
    if site:
        _source(conn, site)
    target = conn.execute(text(
        "SELECT real_source_id FROM legacy_aliases WHERE legacy_version_id=:v AND status='imported'"),
        {"v": record.version_id}).scalar()
    if target:
        _source(conn, target)
    info = alias(conn, record.version_id, mode, cutoff)
    return record.model_copy(update={"migration": info}) if info else record


def visible_versions(source_id, is_demo, mode, cutoff, source_record_ids):
    """每条记录在本模式和截止时间下可见的那个版本 → {source_record_id: version_id}；此时不可见的不在结果里。
    与 list_records 同一套条件（_read_filters：来源许可、未来截止时间、已发布范围；_ranked：截止资格先筛再取最新），
    只是不读载荷。供检索命中的校验用（store.retrieval）；来源不可读、模式不支持时照常报错。"""
    _derived_mode(source_id, mode)
    ids = sorted(set(source_record_ids))
    if not 1 <= len(ids) <= 200:
        raise HistoryError("query_budget", "一次最多核对 200 条记录")
    with history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        source = _source(conn, source_id)
        where, params, _, _ = _read_filters(
            conn, source_id, is_demo, mode, cutoff, source["published_seq"], None, ids, (), None)
        rows = conn.execute(text(
            _ranked(where, mode) + "SELECT source_record_id,version_id FROM ranked WHERE n=1"), params).mappings()
        return {row["source_record_id"]: row["version_id"] for row in rows}


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
                "FROM " + _VERSIONS + " WHERE source_id=:source AND is_demo=:demo "
                "AND published_seq<=:max_seq AND source_record_id=ANY(:ids) "
                "AND " + _ROLE + "!='context_reference' "
                "ORDER BY source_record_id," + _LATEST
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
    from fetch.observations import observation
    from fetch.sources import parse_json

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
    from fetch.sources import parse_json
    from graph.publish.reviewed_graph import GraphBundle

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
        body = _evidence_text(conn, original, evidence.locator) if original is not None else ""
        if (
            original is None
            or body[evidence.locator.start : evidence.locator.end] != evidence.quote
            or evidence.locator.end > len(body)
        ):
            raise HistoryError(
                "graph_quote_mismatch", "图证据不属于同截止下可见的冻结原文"
            )

    from graph.publish.native_graph import validate_field_bindings

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
    from fetch.sources import parse_json
    from graph.publish.reviewed_graph import GRAPH_SOURCES, GraphBundle, GraphRef, resolve

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
    from graph.publish.reviewed_graph import GRAPH_SOURCES, GraphBundle, GraphRef

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
    from graph.publish.graph_tools import graph_identity, paths

    bundle, ref = _fixed_graph(version_id, source_id, source_record_id, is_demo, mode, cutoff)
    return paths(bundle, ref, graph_identity(start), graph_identity(target),
                 relation_types=relation_types, direction=direction, review_scope=review_scope,
                 max_depth=max_depth, max_paths=max_paths, max_expansions=max_expansions)


def read_graph_subgraph(version_id, source_id, source_record_id, is_demo, mode, cutoff, *,
                        node_ids, relation_types=(), review_scope="reviewed",
                        max_nodes=100, max_edges=200):
    from graph.publish.graph_tools import graph_identity, subgraph

    bundle, ref = _fixed_graph(version_id, source_id, source_record_id, is_demo, mode, cutoff)
    return subgraph(bundle, ref, [graph_identity(value) for value in node_ids],
                    relation_types=relation_types, review_scope=review_scope,
                    max_nodes=max_nodes, max_edges=max_edges)
