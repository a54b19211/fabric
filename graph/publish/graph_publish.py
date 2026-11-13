"""明确范围、可续批次的只读图导出和history发布；不触发旧审核、同步或采集。"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from graph.publish.reviewed_graph import (
    GraphBundle,
    GraphEdge,
    GraphEvidence,
    GraphIdentity,
    GraphLocator,
    GraphNode,
    GraphRedirect,
    GraphReview,
    PrimeKGQuery,
    identity_key,
    primekg_bundle,
)
from store import history as store
from store.history_models import (
    ArchiveImport,
    ArchiveRecord,
    HistoryError,
    canonical,
    sha256,
    source_time,
)


def _private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _loopback_url(value):
    parts = urlsplit(value)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise HistoryError(
            "invalid_legacy_origin", "旧图只读入口必须为明确的本机HTTP根地址"
        )
    return value.rstrip("/")


def _read_primekg(statement, parameters, columns, max_rows, *, client=None):
    """两个固定图工具共用的只读HTTP边界；不接受外部传入语句。"""
    import re

    url = os.environ.get("DATAFABRIC_GRAPH_NEO4J_URL", "")
    user = os.environ.get("DATAFABRIC_GRAPH_NEO4J_USER", "")
    password = os.environ.get("DATAFABRIC_GRAPH_NEO4J_PASSWORD", "")
    database = os.environ.get("DATAFABRIC_GRAPH_NEO4J_DATABASE", "neo4j")
    if not url or not user or not password:
        raise HistoryError(
            "primekg_not_configured", "须明确配置图只读地址及独立私有凭据", 503
        )
    url = _loopback_url(url)
    with store.history_engine().begin() as conn:
        store._source(conn, "primekg_graph")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", database):
        raise HistoryError("invalid_neo4j_database", "图数据库名无效")
    body = {"statements": [{
        "statement": statement, "parameters": parameters, "resultDataContents": ["row"]
    }]}
    owned = client is None
    if owned:
        client = httpx.Client(
            base_url=url,
            auth=(user, password),
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        )
    started = time.monotonic()
    try:
        chunks, size = [], 0
        with client.stream(
            "POST",
            "/db/" + database + "/tx/commit",
            json=body,
            headers={"Access-Mode": "READ"},
            timeout=10,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 2 * 1024 * 1024 or time.monotonic() - started > 20:
                    raise HistoryError("primekg_read_budget", "图读取超过2MiB/20秒预算")
                chunks.append(chunk)
        data = json.loads(b"".join(chunks))
        if (
            not isinstance(data, dict)
            or data.get("errors") != []
            or not isinstance(data.get("results"), list)
            or len(data["results"]) != 1
        ):
            raise HistoryError(
                "primekg_query_failed",
                "图服务返回失败或协议不完整，不能当作空邻域",
                503,
            )
        result = data["results"][0]
        rows = [item["row"] for item in result["data"]]
        if (
            result.get("columns") != columns
            or not isinstance(result["data"], list)
            or len(rows) > max_rows
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
        ):
            raise HistoryError("primekg_invalid_response", "图服务响应不符合固定读取契约", 503)
        return rows
    except httpx.TimeoutException:
        raise HistoryError(
            "primekg_timeout", "当前图读取超时，未发布；可重试本次请求", 504
        ) from None
    except httpx.HTTPError:
        raise HistoryError(
            "primekg_read_failed", "当前图HTTP读取失败，未发布，不当作空结果", 503
        ) from None
    except (KeyError, TypeError, ValueError):
        raise HistoryError(
            "primekg_invalid_response", "当前图返回无法核对的结构，未发布", 503
        ) from None
    finally:
        if owned:
            client.close()


def capture_primekg(
    *,
    node_type,
    node_source,
    seed_id,
    relation_types=(),
    neighbor_term="",
    limit=20,
    client=None,
):
    """固定参数化一跳只读查询；复用httpx，不接收Cypher或写入命令。"""
    from datetime import datetime, timezone

    labels = {
        "gene/protein": "`gene/protein`",
        "disease": "`disease`",
        "drug": "`drug`",
    }
    if (
        node_type not in labels
        or not node_source
        or not seed_id
        or len(node_source) > 160
        or len(seed_id) > 400
        or not 1 <= limit <= 200
        or len(relation_types) > 16
        or any(not value or len(value) > 120 for value in relation_types)
        or len(neighbor_term) > 120
    ):
        raise HistoryError("primekg_capture_budget", "PrimeKG身份、类型或读取预算无效")
    query = PrimeKGQuery(
        node_type=node_type, node_source=node_source, seed_id=seed_id,
        relation_types=list(relation_types), neighbor_term=neighbor_term, limit=limit,
    )
    # label只能从上面固定白名单选择；其余全部以参数传入。
    statement = (
        "MATCH (candidate:" + labels[node_type] + " {primekg_id:$seed_id}) "
        "WHERE candidate.node_source=$node_source "
        "WITH candidate LIMIT 2 WITH collect(candidate) AS seeds "
        "UNWIND CASE WHEN size(seeds)=1 THEN seeds ELSE [null] END AS s "
        "OPTIONAL MATCH (s)-[r]-(n) "
        "WHERE (size($relation_types)=0 OR type(r) IN $relation_types) "
        "AND ($neighbor_term='' OR toLower(n.name) CONTAINS toLower($neighbor_term)) "
        "RETURN size(seeds) AS seed_count,properties(s) AS seed,"
        "properties(startNode(r)) AS from_node,properties(endNode(r)) AS to_node,"
        "type(r) AS relation_type,properties(r) AS properties LIMIT $limit_plus_one"
    )
    body = {
        "statements": [
            {
                "statement": statement,
                "parameters": {
                    "seed_id": seed_id,
                    "node_source": node_source,
                    "relation_types": list(relation_types),
                    "neighbor_term": neighbor_term,
                    "limit_plus_one": limit + 1,
                },
                "resultDataContents": ["row"],
            }
        ]
    }
    rows = _read_primekg(
        statement, body["statements"][0]["parameters"],
        ["seed_count", "seed", "from_node", "to_node", "relation_type", "properties"],
        limit + 1, client=client,
    )
    try:
        if not rows:
            raise HistoryError("primekg_invalid_response", "图服务缺少明确身份查询结果", 503)
        if any(row[0] != rows[0][0] for row in rows) or rows[0][0] not in {0, 1}:
            raise HistoryError(
                "ambiguous_primekg_seed", "源命名空间/ID命中多个物理节点，请唐先核对身份"
            )
        seed_found = rows[0][0] == 1

        def node(value):
            if not isinstance(value, dict) or any(
                not isinstance(value.get(key), str) or not value[key]
                for key in ("name", "node_source", "primekg_id", "primekg_type")
            ):
                raise HistoryError(
                    "primekg_invalid_node", "图节点缺少原生命名空间/ID/类型", 503
                )
            return {
                "name": value["name"],
                "source": value["node_source"],
                "id": value["primekg_id"],
                "type": value["primekg_type"],
            }

        seed_node = node(rows[0][1]) if seed_found else None
        projected = []
        for row in rows:
            if row[4] is None:
                if row[2] is not None or row[3] is not None or row[5] is not None:
                    raise HistoryError(
                        "primekg_invalid_response", "图服务空关系状态矛盾", 503
                    )
                continue
            if (
                not seed_found
                or node(row[1]) != seed_node
                or not isinstance(row[4], str)
                or not isinstance(row[5], dict)
            ):
                raise HistoryError(
                    "primekg_invalid_response", "图服务身份/关系状态矛盾", 503
                )
            projected.append(
                {
                    "seed": seed_node,
                    "from_node": node(row[2]),
                    "to_node": node(row[3]),
                    "relation_type": row[4],
                    "properties": row[5],
                }
            )
        return {
            "is_demo": False,
            "source": "primekg_current_projection",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "query": query.scope(),
            "query_parameters": query.model_dump(),
            "seed": {"node_source": node_source, "primekg_id": seed_id},
            "seed_found": seed_found,
            "seed_node": seed_node,
            "limit": limit,
            "has_more": len(projected) > limit,
            "rows": projected[:limit],
            "historical_availability": "unknown",
            "evidence_status": "background_no_document_binding",
        }
    except (KeyError, TypeError, ValueError):
        raise HistoryError(
            "primekg_invalid_response", "当前图返回无法核对的结构，未发布", 503
        ) from None


def _evidence(original, ref, quote):
    if not quote or not isinstance(quote, str):
        return None
    start = original.original_raw_content.find(quote)
    if start < 0 or len(quote) > 8000 or original.original_raw_content.find(quote, start + 1) >= 0:
        return None
    return GraphEvidence(
        **ref.model_dump(),
        quote=quote,
        locator=GraphLocator(start=start, end=start + len(quote)),
    )


def _auditor(actor, at):
    return bool(actor and str(actor).strip() and at
                and str(actor).strip().casefold() not in {"auto", "system", "agent", "unknown"})


def _item_evidence(item, original, ref):
    """确认依据只能用当时核过的那个版本；旧source_text仅供候选展示。"""
    for proof in item.get("current_review_evidence") or []:
        if (proof.get("version_id") == ref.version_id
                and proof.get("source_id") == ref.source_id
                and proof.get("source_record_id") == ref.source_record_id
                and proof.get("raw_sha256") == ref.raw_sha256
                and proof.get("source_doc_uuid") == original.source_doc_uuid):
            bound = _evidence(original, ref, proof.get("quote"))
            if bound is not None:
                return bound, True
    for proof in item.get("evidence") or []:
        if proof.get("source_doc_uuid") == original.source_doc_uuid:
            bound = _evidence(original, ref, proof.get("quote"))
            if bound is not None:
                return bound, False
    return _evidence(original, ref, item.get("source_text")), False


def _bundle_scope(observation, documents, document_refs):
    """The observation's namespace and the selected originals and adoption refs, keyed by document UUID."""
    if observation.get("schema_version") != "datafabric.legacy.graph.observation.v1":
        raise HistoryError("legacy_observation_schema", "不支持的旧图观测格式")
    namespace = observation["namespace"]
    GraphIdentity(namespace=namespace, id="namespace-check")
    doc_map = {doc.source_doc_uuid: doc for doc in documents}
    ref_map = {ref.origin_doc_uuid: ref for ref in document_refs}
    selected = observation["selected_document_uuids"]
    if (
        len(selected) > 6
        or set(doc_map) != set(selected)
        or set(ref_map) != set(selected)
    ):
        raise HistoryError(
            "legacy_document_scope_mismatch", "本批所选UUID与原件/采用清单不一致"
        )
    return namespace, doc_map, ref_map, selected


def _item_scope(item, doc_map, message):
    """The document UUID and review status of an observed entity or relation inside the selected scope."""
    uuid, status = item.get("observation_source_doc_uuid") or item["source_doc_uuid"], item["review_status"]
    if uuid not in doc_map or status not in {"approved", "auto_approved", "pending"}:
        raise HistoryError("legacy_review_scope_mismatch", message)
    return uuid, status


def _identity_confirmed(item, status, dict_id, fixed_proof, context):
    """A mention is a confirmed dictionary identity only with object, dictionary and fixed-text review."""
    return (
        status == "approved" and dict_id is not None and fixed_proof
        and context.get("id") == dict_id and context.get("is_verified") is True
        and _auditor(context.get("verified_by"), context.get("verified_at"))
        and bool(context.get("verification_basis"))
        and _auditor(item.get("identity_confirmed_by"), item.get("identity_confirmed_at"))
        and bool(item.get("identity_confirmation_basis"))
    )


def _new_entity_node(identity, item, confirmed, dict_id, review):
    return GraphNode(
        **identity.model_dump(),
        entity_type=item["entity_type"],
        name=item["entity_name"],
        aliases=[],
        external_ids={},
        identity_status="confirmed" if confirmed else "unresolved",
        identity_review_basis=(
            item["identity_confirmation_basis"]
            if confirmed
            else "source_proposed_dict_id:" + str(dict_id) + ";unverified"
            if dict_id is not None
            else None
        ),
        review=review,
        properties={key: item[key] for key in ("first_seen_at", "valid_from", "valid_to") if item.get(key) is not None},
        mention_ids=[],
        evidence=[],
    )


def _merge_entity_mention(node, item, namespace, confirmed, review, evidence):
    """Fold one more mention into its node: the confirmation, the alias, the mention ID and the evidence."""
    if node.entity_type != item["entity_type"]:
        raise HistoryError(
            "ambiguous_legacy_type", "同一字典身份的实体类型矛盾，不能按名称合并"
        )
    if confirmed:
        node.identity_status = "confirmed"
        node.identity_review_basis = item["identity_confirmation_basis"]
        node.review = review
    if item["entity_name"] != node.name and item["entity_name"] not in node.aliases:
        node.aliases.append(item["entity_name"])
    mention_id = namespace + ".extracted_entity:" + str(item["id"])
    if mention_id not in node.mention_ids:
        node.mention_ids.append(mention_id)
    if evidence is not None and evidence not in node.evidence:
        node.evidence.append(evidence)


def _add_entity(item, namespace, doc_map, ref_map, dictionary_context, mentions, nodes):
    """Record one observed entity as a mention and fold it into its node; False when the node cap cut it off."""
    uuid, status = _item_scope(item, doc_map, "实体不在所选文档或审核范围")
    mention_key = (uuid, item["id"])
    if mention_key in mentions:
        raise HistoryError(
            "duplicate_legacy_mention",
            "源实体ID重复或读取期间状态漂移，请重新捕获本批",
        )
    dict_id = item.get("dict_id")
    evidence, fixed_proof = _item_evidence(item, doc_map[uuid], ref_map[uuid])
    context = item.get("dict_context") or dictionary_context.get(str(dict_id)) or {}
    confirmed = _identity_confirmed(item, status, dict_id, fixed_proof, context)
    review = GraphReview(
        status=status,
        reviewed_by=item.get("identity_confirmed_by") if confirmed else item.get("reviewed_by") or None,
        reviewed_at=item.get("identity_confirmed_at") if confirmed else item.get("reviewed_at"),
    )
    # 自动提议的dict_id不是已确认映射，不能污染人工已确认节点的别名/证据。
    node_namespace = namespace + (
        ".entity_dict" if confirmed else ".extracted_entity"
    )
    node_id = str(dict_id if confirmed else item["id"])
    identity = GraphIdentity(namespace=node_namespace, id=node_id)
    mentions[mention_key] = identity
    key = identity_key(identity)
    if key not in nodes and len(nodes) >= 100:
        return False
    node = nodes.get(key)
    if node is None:
        node = _new_entity_node(identity, item, confirmed, dict_id, review)
        nodes[key] = node
    _merge_entity_mention(node, item, namespace, confirmed, review, evidence)
    return True


def _bundle_nodes(observation, namespace, doc_map, ref_map):
    """Mentions by (document UUID, entity ID), nodes by identity key, and whether the node cap cut any off."""
    dictionary_context = observation.get("dictionary_context") or {}
    mentions, nodes = {}, {}
    truncated = False
    for item in sorted(observation["entities"], key=lambda item: int(item["id"])):
        if not _add_entity(item, namespace, doc_map, ref_map, dictionary_context, mentions, nodes):
            truncated = True
    return mentions, nodes, truncated


def _relation_edge(item, namespace, uuid, status, source, target, doc_map, ref_map):
    props = item.get("relation_props") or {}
    evidence, fixed_proof = _item_evidence(item, doc_map[uuid], ref_map[uuid])
    relation_reviewed = fixed_proof and _auditor(item.get("reviewed_by"), item.get("reviewed_at"))
    return GraphEdge(
        id=namespace + ".relation:" + str(item["id"]) + ":" + sha256(uuid)[:16],
        source=source,
        target=target,
        relation_type=item["relation_type"],
        relation_props=props,
        valid_from=source_time(
            item.get("valid_from") or props.get("start_date"), "source_relation_start"
        ),
        valid_to=source_time(item.get("valid_to") or props.get("end_date"), "source_relation_end"),
        review=GraphReview(status=status, reviewed_by=item.get("reviewed_by") if relation_reviewed else None,
                           reviewed_at=item.get("reviewed_at") if relation_reviewed else None),
        source_doc_uuid=uuid,
        binding_status="bound" if evidence is not None else "unbound",
        evidence=[evidence] if evidence is not None else [],
        unbound_reason=None
        if evidence is not None
        else "旧source_text不能在本次冻结原文逐字定位",
    )


def _bundle_edges(observation, namespace, doc_map, ref_map, mentions, nodes):
    """The edges between kept nodes (at most 200), and whether any relation was left out."""
    edges, truncated = [], False
    for item in sorted(observation["relations"], key=lambda item: int(item["id"])):
        uuid, status = _item_scope(item, doc_map, "关系不在所选文档或审核范围")
        source, target = (
            mentions.get((uuid, item.get("from_entity_id"))),
            mentions.get((uuid, item.get("to_entity_id"))),
        )
        if (
            source is None
            or target is None
            or identity_key(source) not in nodes
            or identity_key(target) not in nodes
        ):
            truncated = True
            continue
        if len(edges) >= 200:
            truncated = True
            break
        edges.append(_relation_edge(item, namespace, uuid, status, source, target, doc_map, ref_map))
    return edges, truncated


def _bundle_redirects(observation, namespace, nodes):
    # 只接纳导出中明确给出的未回滚日志；默认HTTP捕获不读取全字典别名或全量合并历史。
    redirects = []
    for item in observation.get("merge_logs", []):
        target = GraphIdentity(
            namespace=namespace + ".entity_dict", id=str(item["keeper_dict_id"])
        )
        if (item.get("is_rolled_back") is not False or identity_key(target) not in nodes
                or not _auditor(item.get("reviewer"), item.get("created_at"))):
            continue
        for absorbed in item["absorbed_dict_ids"]:
            redirects.append(
                GraphRedirect(
                    source=GraphIdentity(
                        namespace=namespace + ".entity_dict", id=str(absorbed)
                    ),
                    target=target,
                    merge_log_id=str(item["id"]),
                    reviewed_by=item.get("reviewer") or None,
                    merged_at=item.get("created_at"),
                )
            )
    return redirects


def build_document_bundle(observation, documents, document_refs):
    namespace, doc_map, ref_map, selected = _bundle_scope(observation, documents, document_refs)
    truncated = observation.get("selection_truncated") is True
    mentions, nodes, nodes_cut = _bundle_nodes(observation, namespace, doc_map, ref_map)
    edges, edges_cut = _bundle_edges(observation, namespace, doc_map, ref_map, mentions, nodes)
    truncated = truncated or nodes_cut or edges_cut
    redirects = _bundle_redirects(observation, namespace, nodes)
    return GraphBundle(
        graph_kind="reviewed",
        namespace=namespace,
        is_demo=observation.get("is_demo") is True,
        title="唐的所选文档当前图观测",
        origin="explicit_demo"
        if observation.get("is_demo") is True
        else "legacy_reviewed_export",
        scope_description="明确UUID清单的本批有界导出；未扫描或代表完整来源/全图",
        scope_record_ids=observation["scope_record_ids"],
        scope_manifest_sha256=observation.get("scope_manifest_sha256"),
        scope_record_count=observation.get(
            "scope_record_count", len(observation["scope_record_ids"])
        ),
        batch_offset=observation.get("batch_offset", 0),
        selected_document_uuids=selected,
        next_document_offset=observation.get("next_document_offset"),
        # 依赖 = 引用到的版本所属来源；documents 库存的冻结版本再加它的原站许可
        dependency_source_ids=sorted(
            {ref.source_id for ref in document_refs}
            | {doc_map[ref.origin_doc_uuid].source_site for ref in document_refs
               if ref.source_id == "legacy_documents"}
        ),
        document_refs=document_refs,
        selection_truncated=truncated,
        nodes=list(nodes.values()),
        edges=edges,
        redirects=redirects,
        limitations=[
            "人工确认身份须同时有对象确认、字典审核和本次固定原文证据；旧verified标志不足以确认",
            "auto_approved仍为自动审核且身份未确认；别名只来自本批mention原名，不披露全字典汇总",
            "pending关系仅为未审核候选；原文绑定与事实审核是两件事，不能据此升级为人工审核或机制证据",
            "当前别名/合并没有历史资格；有效时间仅保留来源关系属性，未知不取文档日期",
            "无审核关系只说明此所选范围缺少可用关系，不代表现实不存在",
        ],
    )


def bind_document_text(bundle, versions):
    """原MD与清洗正文可能偏移；只在相同冻结版本的正文中重新定位唯一原句。"""
    def bound(items):
        result = []
        for evidence in items:
            record = versions[evidence.version_id]
            start = record.clean_text.find(evidence.quote)
            if start >= 0 and record.clean_text.find(evidence.quote, start + 1) < 0:
                result.append(evidence.model_copy(update={
                    "locator": GraphLocator(start=start, end=start + len(evidence.quote))}))
        return result

    for node in bundle.nodes:
        node.evidence = bound(node.evidence)
        if node.identity_status == "confirmed" and not node.evidence:
            node.identity_status = "unresolved"
            node.identity_review_basis = "confirmed_source_not_bound_to_current_text"
    for edge in bundle.edges:
        edge.evidence = bound(edge.evidence)
        if not edge.evidence:
            edge.binding_status = "unbound"
            edge.unbound_reason = "来源句子在已采用正文中缺失或多处出现，未作模糊定位"
    return bundle


def publish_native_graph(records, key, *, max_nodes=100, max_edges=200):
    from graph.publish.native_graph import build_native_graph

    bundle = build_native_graph(records, max_nodes=max_nodes, max_edges=max_edges)
    native = "source-fields:" + sha256(canonical({
        "namespace": bundle.namespace, "scope": sorted(bundle.scope_record_ids),
        "max_nodes": max_nodes, "max_edges": max_edges,
    }))
    return store.import_archive(ArchiveImport(
        source_id="reviewed_graph", is_demo=bundle.is_demo,
        scope_description=bundle.scope_description, declared_count=1, declared_complete=False,
        records=[ArchiveRecord(source_record_id=native, record_kind="graph", raw_content=bundle.model_dump_json())],
    ), key)


def publish_primekg(observation, key):
    bundle = primekg_bundle(observation)
    identity = {"namespace": bundle.namespace, "scope": bundle.scope_record_ids}
    if observation.get("query_parameters") is not None:
        identity["query"] = observation["query_parameters"]
    native = "primekg-capture:" + sha256(canonical(identity))
    return store.import_archive(
        ArchiveImport(
            source_id="primekg_graph",
            is_demo=False,
            scope_description=bundle.scope_description,
            declared_count=1,
            declared_complete=False,
            records=[
                ArchiveRecord(
                    source_record_id=native,
                    record_kind="graph",
                    raw_content=bundle.model_dump_json(),
                )
            ],
        ),
        key,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prime_capture = commands.add_parser("capture-primekg")
    prime_capture.add_argument(
        "--node-type",
        choices=["gene/protein", "disease", "drug"],
        default="gene/protein",
    )
    prime_capture.add_argument("--node-source", required=True)
    prime_capture.add_argument("--seed-id", required=True)
    prime_capture.add_argument("--relation-type", action="append", default=[])
    prime_capture.add_argument("--neighbor-term", default="")
    prime_capture.add_argument("--limit", type=int, default=20)
    prime_capture.add_argument("--output", required=True)
    for name in ("publish-primekg",):
        command = commands.add_parser(name)
        command.add_argument("--observation", required=True)
        command.add_argument("--idempotency-key", required=True)
        command.add_argument("--receipt", required=True)
    args = parser.parse_args()
    if args.command == "capture-primekg":
        _private_json(
            args.output, {"state": "reading", "source": "primekg_current_projection"}
        )
        try:
            observation = capture_primekg(
                node_type=args.node_type,
                node_source=args.node_source,
                seed_id=args.seed_id,
                relation_types=args.relation_type,
                neighbor_term=args.neighbor_term,
                limit=args.limit,
            )
        except HistoryError as exc:
            Path(args.output).write_text(
                canonical({"state": "failed", "code": exc.code, "message": exc.message})
            )
            raise SystemExit(exc.message) from None
        Path(args.output).write_text(canonical(observation))
        print(
            canonical(
                {
                    "state": "captured",
                    "seed_found": observation["seed_found"],
                    "physical_edges": len(observation["rows"]),
                    "has_more": observation["has_more"],
                }
            )
        )
        return
    if len(args.idempotency_key) > 130:
        raise SystemExit("幂等键前缀须不超过130字符")
    observation = json.loads(Path(args.observation).read_text())
    receipt = {
        "state": "publishing",
        "idempotency_key": args.idempotency_key,
        "observation_sha256": sha256(canonical(observation)),
    }
    _private_json(args.receipt, receipt)

    def progress(value):
        receipt.update(value)
        Path(args.receipt).write_text(canonical(receipt))

    try:
        run = publish_primekg(observation, args.idempotency_key)
    except HistoryError as exc:
        receipt.update(state="failed", code=exc.code, message=exc.message)
        Path(args.receipt).write_text(canonical(receipt))
        raise SystemExit(exc.message) from None
    except Exception:  # noqa: BLE001 -- CLI记录未知写入，绝不回显配置/任意异常或自动重试
        receipt.update(
            state="unknown",
            message="数据库写入结果未确认；请按回执任务ID/幂等键核对，不自动重发",
        )
        Path(args.receipt).write_text(canonical(receipt))
        raise SystemExit(receipt["message"]) from None
    progress(
        {
            "state": run.state,
            "run_id": run.id,
            "source_id": run.source_id,
            "version_ids": run.version_ids,
        }
    )
    print(
        canonical(
            {
                key: receipt[key]
                for key in ("state", "run_id", "source_id", "version_ids")
            }
        )
    )


if __name__ == "__main__":
    main()
