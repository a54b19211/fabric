"""固定发布包内的有界路径/子图；不查询外部图、不推断新关系。"""

from collections import deque
from typing import Literal

from biz_rules.history_models import HistoryError, Model
from biz_rules.reviewed_graph import (
    GraphCoverage, GraphEdge, GraphIdentity, GraphNeighborhood, GraphNode,
    GraphRef, identity_key,
)

ReviewScope = Literal["reviewed", "with_candidates"]


def graph_identity(value):
    if not isinstance(value, str) or value.count("|") != 1:
        raise HistoryError("invalid_graph_identity", "身份格式须为namespace|id")
    namespace, identity = value.split("|")
    try:
        return GraphIdentity(namespace=namespace, id=identity)
    except ValueError:
        raise HistoryError("invalid_graph_identity", "图身份无效") from None


def audited_review(review):
    actor = (review.reviewed_by or "").strip()
    return (review.status == "approved" and bool(actor) and review.reviewed_at is not None
            and actor.casefold() not in {"auto", "system", "agent", "unknown"})


def confirmed_identity(node):
    if node.identity_status == "rule_confirmed":
        return node.identity_rule is not None and node.review.status == "rule_confirmed"
    return (node.identity_status == "confirmed" and audited_review(node.review)
            and bool(node.identity_review_basis) and bool(node.evidence or node.field_evidence))


def _selection(bundle, scope):
    if scope not in {"reviewed", "with_candidates"}:
        raise HistoryError("graph_review_scope", "请选择审核事实或显式包含候选")
    statuses = {"approved", "rule_confirmed", "auto_approved", "pending", "background"}
    nodes = {}
    for node in bundle.nodes:
        if node.review.status not in statuses:
            continue
        trusted = confirmed_identity(node)
        if scope == "reviewed" and not trusted:
            continue
        if node.identity_status == "confirmed" and not trusted:
            # 原包不改写；旧confirmed声明在查询结果中明确降为未核候选。
            node = node.model_copy(update={
                "identity_status": "unresolved",
                "identity_review_basis": "source_declared_confirmation_without_audit",
            })
        nodes[identity_key(node)] = node
    edges = [edge for edge in bundle.edges if edge.review.status in statuses
             and identity_key(edge.source) in nodes and identity_key(edge.target) in nodes
             and (scope == "with_candidates" or (
                 edge.binding_status == "bound"
                 and (audited_review(edge.review)
                      or (edge.review.status == "rule_confirmed" and bool(edge.field_evidence)))))]
    return nodes, edges


def select_bundle(bundle, scope):
    nodes, edges = _selection(bundle, scope)
    return bundle.model_copy(update={
        "nodes": list(nodes.values()), "edges": edges,
        "redirects": [item for item in bundle.redirects if identity_key(item.target) in nodes],
        "limitations": [*bundle.limitations, "本次审核状态范围：" + scope + "；状态筛选排除的对象不代表现实不存在"],
    })


class GraphPath(Model):
    nodes: list[GraphIdentity]
    edge_ids: list[str]


class GraphPaths(Model):
    schema_version: Literal["datafabric.graph.paths.v1"] = "datafabric.graph.paths.v1"
    graph_ref: GraphRef
    review_scope: ReviewScope
    status: Literal["found", "not_found"]
    paths: list[GraphPath]
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    seeds_missing: list[GraphIdentity]
    coverage: GraphCoverage
    explored_states: int
    max_expansions: int
    limitations: list[str]


def paths(bundle, graph_ref, start, target, *, relation_types=(), direction="out",
          review_scope="reviewed", max_depth=4, max_paths=10, max_expansions=1000):
    if (direction not in {"out", "in", "both"} or not 1 <= max_depth <= 4
            or not 1 <= max_paths <= 20 or not 1 <= max_expansions <= 5000
            or len(relation_types) > 16):
        raise HistoryError("graph_query_budget", "路径方向/深度/数量/搜索预算无效")
    nodes, edges = _selection(bundle, review_scope)
    source, destination = identity_key(start), identity_key(target)
    missing = [node for node in (start, target) if identity_key(node) not in nodes]
    missing = list({identity_key(node): node for node in missing}.values())
    adjacency = {}
    for edge in sorted(edges, key=lambda value: value.id):
        if relation_types and edge.relation_type not in relation_types:
            continue
        if direction in {"out", "both"}:
            adjacency.setdefault(identity_key(edge.source), []).append((edge, identity_key(edge.target)))
        if direction in {"in", "both"}:
            adjacency.setdefault(identity_key(edge.target), []).append((edge, identity_key(edge.source)))
    queue = deque() if missing else deque([(source, [source], [])])
    result, picked, selected, explored, generated, reason = [], {}, {}, 0, 1, None
    while queue:
        key, visited, used = queue.popleft()
        explored += 1
        if key == destination:
            result.append(GraphPath(nodes=[GraphIdentity(namespace=k[0], id=k[1]) for k in visited],
                                    edge_ids=[edge.id for edge in used]))
            selected.update((key, nodes[key]) for key in visited)
            picked.update((edge.id, edge) for edge in used)
            if len(result) == max_paths:
                if queue:
                    reason = "path_budget"
                break
            continue
        eligible = [(edge, other) for edge, other in adjacency.get(key, ()) if other not in visited]
        if len(used) >= max_depth:
            if eligible and not reason:
                reason = "depth_budget"
            continue
        for edge, other in eligible:
            if generated >= max_expansions:
                reason = "search_budget"
                break
            generated += 1
            queue.append((other, visited + [other], used + [edge]))
    return GraphPaths(graph_ref=graph_ref, review_scope=review_scope,
                      status="found" if result else "not_found", paths=result,
                      nodes=list(selected.values()), edges=list(picked.values()), seeds_missing=missing,
                      coverage=GraphCoverage(truncated=bool(reason) or bundle.selection_truncated,
                                             selection_truncated=bundle.selection_truncated,
                                             stop_reason=reason or ("source_selection_limit" if bundle.selection_truncated else "end_of_selected_paths"),
                                             returned_nodes=len(selected), returned_edges=len(picked), max_depth=max_depth),
                      explored_states=explored, max_expansions=max_expansions,
                      limitations=[*bundle.limitations,
                                   "路径只串联本固定包中的已有边，不证明机制链、因果或独立研究支持",
                                   "reviewed只含人工approved或来源字段rule_confirmed；候选须显式with_candidates，未自动消歧",
                                   "未找到仅限当前版本、状态筛选及搜索预算，不证明现实世界无关系"])


class GraphSubgraph(GraphNeighborhood):
    review_scope: ReviewScope


def subgraph(bundle, graph_ref, node_ids, *, relation_types=(), review_scope="reviewed",
             max_nodes=100, max_edges=200):
    if (not node_ids or len(node_ids) > 100 or len(relation_types) > 16
            or not 1 <= max_nodes <= 100 or not 1 <= max_edges <= 200
            or len({identity_key(node) for node in node_ids}) != len(node_ids)):
        raise HistoryError("graph_query_budget", "子图须给1至100个不重复身份及有效预算")
    nodes, edges = _selection(bundle, review_scope)
    found = [node for node in node_ids if identity_key(node) in nodes]
    missing = [node for node in node_ids if identity_key(node) not in nodes]
    selected = {identity_key(node): nodes[identity_key(node)] for node in found[:max_nodes]}
    eligible = [edge for edge in sorted(edges, key=lambda value: value.id)
                if identity_key(edge.source) in selected and identity_key(edge.target) in selected
                and (not relation_types or edge.relation_type in relation_types)]
    reason = "node_budget" if len(found) > max_nodes else "edge_budget" if len(eligible) > max_edges else None
    return GraphSubgraph(graph_ref=graph_ref, namespace=bundle.namespace, review_scope=review_scope,
                         nodes=list(selected.values()), edges=eligible[:max_edges],
                         redirects=[item for item in bundle.redirects if identity_key(item.target) in selected],
                         seeds_found=found, seeds_missing=missing,
                         coverage=GraphCoverage(truncated=bool(reason) or bundle.selection_truncated,
                                                selection_truncated=bundle.selection_truncated,
                                                stop_reason=reason or ("source_selection_limit" if bundle.selection_truncated else "end_of_requested_subgraph"),
                                                returned_nodes=len(selected), returned_edges=min(len(eligible), max_edges), max_depth=0),
                         limitations=[*bundle.limitations,
                                      "仅返回所列身份在这个固定包中的诱导子图；不自动合并同名对象或跨版本查询",
                                      "审核/规则事实与自动候选保持原状态；未返回可能来自身份、状态筛选或预算限制"])
