"""明确范围、可续批次的只读图导出和history发布；不触发旧审核、同步或采集。"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from biz_rules.history_models import (
    ArchiveImport,
    ArchiveRecord,
    HistoryError,
    canonical,
    sha256,
    source_time,
)
from biz_rules.reviewed_graph import (
    GraphBundle,
    GraphDocumentRef,
    GraphEdge,
    GraphEvidence,
    GraphIdentity,
    GraphLocator,
    GraphNode,
    GraphRedirect,
    GraphReview,
    LegacyDocument,
    document_native_id,
    identity_key,
    primekg_bundle,
)
from db import history as store


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


def capture_legacy(
    base_url,
    scope_ids,
    namespace,
    *,
    offset=0,
    batch_size=6,
    include_auto=False,
    expected_manifest_sha256=None,
    client=None,
):
    """每批至多六篇；完整输入UUID清单和next offset让调用方继续，不把一批当全图。"""
    if (
        not scope_ids
        or len(scope_ids) > 20000
        or len(set(scope_ids)) != len(scope_ids)
        or not 0 <= offset < len(scope_ids)
        or not 1 <= batch_size <= 6
    ):
        raise HistoryError(
            "legacy_scope_budget", "须给不重复明确UUID清单及有效批次范围"
        )
    GraphIdentity(namespace=namespace, id="namespace-check")
    base_url = _loopback_url(base_url)
    owned = client is None
    if owned:
        client = httpx.Client(
            base_url=base_url, timeout=5, follow_redirects=False, trust_env=False
        )
    manifest_sha = sha256(canonical(scope_ids))
    if (offset > 0 and expected_manifest_sha256 != manifest_sha) or (
        expected_manifest_sha256 is not None
        and expected_manifest_sha256 != manifest_sha
    ):
        raise HistoryError(
            "scope_manifest_changed",
            "续批必须绑定同一输入清单SHA；名单变化时不继续读取",
        )
    selected = scope_ids[offset : offset + batch_size]
    documents, entities, relations, verified = [], [], [], set()
    total_bytes = 0
    deadline = time.monotonic() + 60

    def read(path, params=None):
        nonlocal total_bytes
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HistoryError("legacy_read_deadline", "旧图本批读取超过60秒期限")
        with client.stream(
            "GET", path, params=params, timeout=min(5, remaining)
        ) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise HistoryError(
                        "legacy_read_deadline", "旧图本批读取超过60秒期限"
                    )
                size += len(chunk)
                total_bytes += len(chunk)
                if size > 2 * 1024 * 1024 or total_bytes > 8 * 1024 * 1024:
                    raise HistoryError(
                        "legacy_read_budget", "旧图读取超过单响应2MiB/批次8MiB预算"
                    )
                chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks))
        except ValueError:
            raise HistoryError(
                "legacy_invalid_response", "旧图返回不是有效JSON"
            ) from None

    truncated = False
    try:
        for uuid in selected:
            # UUID作为路径段，避免斜杠/查询字符改变精确白名单路径。
            if any(
                char
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:."
                for char in uuid
            ):
                raise HistoryError("invalid_legacy_uuid", "旧文档UUID格式无效")
            document = read("/api/documents/" + uuid)
            if document.get("uuid") != uuid or not isinstance(
                document.get("raw_content"), str
            ):
                raise HistoryError(
                    "legacy_document_mismatch", "旧详情缺少精确UUID或正文"
                )
            with store.history_engine().begin() as conn:
                store._source(conn, document.get("source_site"))
            documents.append(document)
            for status in (
                ["approved", "auto_approved"] if include_auto else ["approved"]
            ):
                for kind, destination in (
                    ("entities", entities),
                    ("relations", relations),
                ):
                    result = read(
                        "/api/review/" + kind,
                        {
                            "source_doc_uuid": uuid,
                            "review_status": status,
                            "page": 1,
                            "page_size": 100,
                        },
                    )
                    items = result.get("items")
                    if not isinstance(items, list) or any(
                        item.get("source_doc_uuid") != uuid
                        or item.get("review_status") != status
                        for item in items
                    ):
                        raise HistoryError(
                            "legacy_review_scope_mismatch",
                            "旧审核列表的文档/状态范围不一致",
                        )
                    destination.extend(items)
                    truncated |= result.get("total", len(items)) > len(items)
            graph = read(
                "/api/review/graph",
                {
                    "source_doc_uuid": uuid,
                    "review_status": "approved,auto_approved",
                    "traversal": "degree",
                    "limit": 100,
                    "include_relations": False,
                },
            )
            verified.update(
                str(node["dict_id"])
                for node in graph.get("nodes", [])
                if node.get("dict_id") is not None
                and node.get("is_verified") is True
                and node.get("review_status") == "approved"
            )
    except httpx.HTTPError:
        raise HistoryError(
            "legacy_read_failed", "旧图HTTP读取失败；本批未发布，不当作空结果", 503
        ) from None
    finally:
        if owned:
            client.close()
    return {
        "schema_version": "datafabric.legacy.graph.observation.v1",
        "namespace": namespace,
        "is_demo": False,
        "scope_record_ids": selected,
        "scope_manifest_sha256": manifest_sha,
        "scope_record_count": len(scope_ids),
        "batch_offset": offset,
        "selected_document_uuids": selected,
        "next_document_offset": offset + len(selected)
        if offset + len(selected) < len(scope_ids)
        else None,
        "selection_truncated": truncated,
        "documents": documents,
        "entities": entities,
        "relations": relations,
        "verified_dict_ids": sorted(verified),
        "merge_logs": [],
    }


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
    import re
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
        expected = [
            "seed_count",
            "seed",
            "from_node",
            "to_node",
            "relation_type",
            "properties",
        ]
        if (
            result.get("columns") != expected
            or not isinstance(result.get("data"), list)
            or len(result["data"]) > limit + 1
        ):
            raise HistoryError(
                "primekg_invalid_response", "图服务响应不符合固定读取契约", 503
            )
        rows = [item["row"] for item in result["data"]]
        if not rows or any(not isinstance(row, list) or len(row) != 6 for row in rows):
            raise HistoryError(
                "primekg_invalid_response", "图服务缺少明确身份查询结果", 503
            )
        if any(row[0] != rows[0][0] for row in rows) or rows[0][0] not in {0, 1}:
            raise HistoryError(
                "ambiguous_primekg_seed",
                "源命名空间/ID命中多个物理节点，请唐先核对身份",
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
            "query": "bounded one-hop "
            + node_type
            + " source="
            + node_source
            + " id="
            + seed_id
            + " neighbor_literal="
            + neighbor_term,
            "seed": {"node_source": node_source, "primekg_id": seed_id},
            "seed_found": seed_found,
            "seed_node": seed_node,
            "limit": limit,
            "has_more": len(projected) > limit,
            "rows": projected[:limit],
            "historical_availability": "unknown",
            "evidence_status": "background_no_document_binding",
        }
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


def _legacy_document(value):
    return LegacyDocument(
        source_doc_uuid=value["uuid"],
        source_site=value["source_site"],
        title=value.get("title") or value["uuid"],
        source_url=value.get("source_url") or None,
        original_raw_content=value["raw_content"],
        original_raw_sha256=sha256(value["raw_content"]),
        publish_date_raw=value.get("publish_date") or None,
        original_created_at=str(value["created_at"])
        if value.get("created_at")
        else None,
        original_updated_at=str(value["updated_at"])
        if value.get("updated_at")
        else None,
    )


def _evidence(original, ref, quote):
    if not quote or not isinstance(quote, str):
        return None
    start = original.original_raw_content.find(quote)
    if start < 0 or len(quote) > 8000:
        return None
    return GraphEvidence(
        **ref.model_dump(),
        quote=quote,
        locator=GraphLocator(start=start, end=start + len(quote)),
    )


def build_legacy_bundle(observation, documents, document_refs):
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
    verified = set(observation.get("verified_dict_ids", []))
    mentions, nodes, edges = {}, {}, []
    truncated = observation.get("selection_truncated") is True
    for item in sorted(observation["entities"], key=lambda item: int(item["id"])):
        uuid, status = item["source_doc_uuid"], item["review_status"]
        if uuid not in doc_map or status not in {"approved", "auto_approved"}:
            raise HistoryError(
                "legacy_review_scope_mismatch", "实体不在所选文档或审核范围"
            )
        if item["id"] in mentions:
            raise HistoryError(
                "duplicate_legacy_mention",
                "源实体ID重复或读取期间状态漂移，请重新捕获本批",
            )
        dict_id = item.get("dict_id")
        confirmed = (
            status == "approved" and dict_id is not None and str(dict_id) in verified
        )
        # 自动提议的dict_id不是已确认映射，不能污染人工已确认节点的别名/证据。
        node_namespace = namespace + (
            ".entity_dict" if confirmed else ".extracted_entity"
        )
        node_id = str(dict_id if confirmed else item["id"])
        identity = GraphIdentity(namespace=node_namespace, id=node_id)
        mentions[item["id"]] = identity
        key = identity_key(identity)
        if key not in nodes and len(nodes) >= 100:
            truncated = True
            continue
        evidence = _evidence(doc_map[uuid], ref_map[uuid], item.get("source_text"))
        node = nodes.get(key)
        if node is None:
            node = GraphNode(
                **identity.model_dump(),
                entity_type=item["entity_type"],
                name=item["entity_name"],
                aliases=[],
                external_ids={},
                identity_status="confirmed" if confirmed else "unresolved",
                identity_review_basis=(
                    "legacy_approved_and_verified_dict"
                    if confirmed
                    else "source_proposed_dict_id:" + str(dict_id) + ";unverified"
                    if dict_id is not None
                    else None
                ),
                review=GraphReview(status=status),
                mention_ids=[],
                evidence=[],
            )
            nodes[key] = node
        if node.entity_type != item["entity_type"]:
            raise HistoryError(
                "ambiguous_legacy_type", "同一字典身份的实体类型矛盾，不能按名称合并"
            )
        if confirmed:
            node.identity_status = "confirmed"
            node.identity_review_basis = "legacy_approved_and_verified_dict"
            node.review = GraphReview(status="approved")
        if item["entity_name"] != node.name and item["entity_name"] not in node.aliases:
            node.aliases.append(item["entity_name"])
        node.mention_ids.append(namespace + ".extracted_entity:" + str(item["id"]))
        if evidence is not None and evidence not in node.evidence:
            node.evidence.append(evidence)
    for item in sorted(observation["relations"], key=lambda item: int(item["id"])):
        uuid, status = item["source_doc_uuid"], item["review_status"]
        if uuid not in doc_map or status not in {"approved", "auto_approved"}:
            raise HistoryError(
                "legacy_review_scope_mismatch", "关系不在所选文档或审核范围"
            )
        source, target = (
            mentions.get(item.get("from_entity_id")),
            mentions.get(item.get("to_entity_id")),
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
        props = item.get("relation_props") or {}
        evidence = _evidence(doc_map[uuid], ref_map[uuid], item.get("source_text"))
        edges.append(
            GraphEdge(
                id=namespace + ".relation:" + str(item["id"]),
                source=source,
                target=target,
                relation_type=item["relation_type"],
                relation_props=props,
                valid_from=source_time(
                    props.get("start_date"), "source_relation_start"
                ),
                valid_to=source_time(props.get("end_date"), "source_relation_end"),
                review=GraphReview(status=status),
                source_doc_uuid=uuid,
                binding_status="bound" if evidence is not None else "unbound",
                evidence=[evidence] if evidence is not None else [],
                unbound_reason=None
                if evidence is not None
                else "旧source_text不能在本次冻结原文逐字定位",
            )
        )
    # 只接纳导出中明确给出的未回滚日志；默认HTTP捕获不读取全字典别名或全量合并历史。
    redirects = []
    for item in observation.get("merge_logs", []):
        target = GraphIdentity(
            namespace=namespace + ".entity_dict", id=str(item["keeper_dict_id"])
        )
        if item.get("is_rolled_back") is not False or identity_key(target) not in nodes:
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
        dependency_source_ids=sorted(
            {"legacy_documents"} | {doc.source_site for doc in documents}
        ),
        document_refs=document_refs,
        selection_truncated=truncated,
        nodes=list(nodes.values()),
        edges=edges,
        redirects=redirects,
        limitations=[
            "身份状态来自源系统当前审核/字典标志，不是机制证据评价或专业批准",
            "auto_approved仍为自动审核且身份未确认；别名只来自本批mention原名，不披露全字典汇总",
            "当前别名/合并没有历史资格；有效时间仅保留来源关系属性，未知不取文档日期",
            "无审核关系只说明此所选范围缺少可用关系，不代表现实不存在",
        ],
    )


def publish_legacy(observation, key, progress=None):
    documents = [_legacy_document(value) for value in observation["documents"]]
    if not documents or len(documents) > 6:
        raise HistoryError("legacy_batch_budget", "每批须有1至6篇明确旧文档")
    request = ArchiveImport(
        source_id="legacy_documents",
        is_demo=observation.get("is_demo") is True,
        scope_description="明确旧UUID本批原文冻结，不赋历史资格",
        declared_count=len(documents),
        declared_complete=True,
        records=[
            ArchiveRecord(
                source_record_id=document_native_id(doc),
                record_kind="document",
                raw_content=doc.model_dump_json(),
            )
            for doc in documents
        ],
    )
    run = store.import_archive(request, key + ":documents")
    if progress:
        progress({"document_run_id": run.id, "document_version_ids": run.version_ids})
    with store.history_engine().begin() as conn:
        now = store._now(conn)
    refs = []
    for version_id, doc in zip(run.version_ids, documents, strict=True):
        record = store.read_version(
            version_id,
            "legacy_documents",
            document_native_id(doc),
            request.is_demo,
            "current",
            now,
        )
        refs.append(
            GraphDocumentRef(
                source_id=record.source_id,
                source_record_id=record.source_record_id,
                version_id=record.version_id,
                raw_sha256=record.raw_sha256,
                origin_doc_uuid=doc.source_doc_uuid,
            )
        )
    bundle = build_legacy_bundle(observation, documents, refs)
    native = "legacy-graph:" + sha256(
        canonical(
            {
                "namespace": bundle.namespace,
                "scope": bundle.scope_manifest_sha256 or bundle.scope_record_ids,
                "offset": bundle.batch_offset,
                "selected": bundle.selected_document_uuids,
            }
        )
    )
    graph_run = store.import_archive(
        ArchiveImport(
            source_id="reviewed_graph",
            is_demo=bundle.is_demo,
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
        key + ":graph",
    )
    return graph_run


def publish_primekg(observation, key):
    bundle = primekg_bundle(observation)
    native = "primekg-capture:" + sha256(
        canonical({"namespace": bundle.namespace, "scope": bundle.scope_record_ids})
    )
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
    capture = commands.add_parser("capture-legacy")
    capture.add_argument("--url", required=True)
    capture.add_argument("--namespace", required=True)
    capture.add_argument("--scope-file", required=True)
    capture.add_argument("--offset", type=int, default=0)
    capture.add_argument("--batch-size", type=int, default=6)
    capture.add_argument("--include-auto-approved", action="store_true")
    capture.add_argument("--expected-scope-sha256")
    capture.add_argument("--output", required=True)
    for name in ("publish-legacy", "publish-primekg"):
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
    if args.command == "capture-legacy":
        scope = json.loads(Path(args.scope_file).read_text())
        # 先占用输出路径，读失败也留下明确回执，不覆盖旧观测。
        _private_json(args.output, {"state": "reading", "offset": args.offset})
        try:
            result = capture_legacy(
                args.url,
                scope,
                args.namespace,
                offset=args.offset,
                batch_size=args.batch_size,
                include_auto=args.include_auto_approved,
                expected_manifest_sha256=args.expected_scope_sha256,
            )
        except HistoryError as exc:
            Path(args.output).write_text(
                canonical({"state": "failed", "code": exc.code, "message": exc.message})
            )
            raise SystemExit(exc.message) from None
        Path(args.output).write_text(canonical(result))
        print(
            canonical(
                {
                    "state": "captured",
                    "documents": len(result["documents"]),
                    "scope_manifest_sha256": result["scope_manifest_sha256"],
                    "scope_record_count": result["scope_record_count"],
                    "next_document_offset": result["next_document_offset"],
                    "selection_truncated": result["selection_truncated"],
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
        run = (
            publish_legacy(observation, args.idempotency_key, progress)
            if args.command == "publish-legacy"
            else publish_primekg(observation, args.idempotency_key)
        )
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
