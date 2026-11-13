"""固定图版本（reviewed_graph、primekg_graph）在 History 库里的读取和发布前核对：图包引用的原件、证据引文、
邻域/解析/路径/子图。原在 store/history.py，唐组维护；雷达只经 /graph/{version_id}/resolve 读 read_graph_resolution。"""

from __future__ import annotations

from sqlalchemy import text

from store import history as store
from store.history_models import HistoryError


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
    from fetch.catalog import RETIRED

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
        store._source(conn, ref.source_id)
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
            store._source(conn, original_source)
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
    record = store.read_version(
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


def _check_graph_document_visibility(conn, record, mode, cutoff):
    from fetch.sources import parse_json
    from graph.publish.reviewed_graph import GraphBundle

    bundle = GraphBundle.model_validate(parse_json(record.raw_content))
    documents = {}
    for ref in bundle.document_refs:
        store._derived_mode(ref.source_id, mode)
        store._source(conn, ref.source_id)
        row = conn.execute(
            text(
                "SELECT payload FROM history_versions WHERE version_id=:id AND source_id=:source AND source_record_id=:native AND is_demo=:demo AND "
                + store._eligibility(mode)
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
    record = store.read_version(
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
    record = store.read_version(version_id, source_id, source_record_id, is_demo, mode, cutoff)
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
