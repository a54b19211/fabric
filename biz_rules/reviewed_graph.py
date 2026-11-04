"""唐的当前图导出契约与固定版本有界遍历；不消歧、不调用模型或外部服务。"""

from __future__ import annotations

from processing.identifiers import NCT_ID
from collections import deque
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from biz_rules.history_models import (
    HistoryError,
    Model,
    TimeValue,
    canonical,
    sha256,
    utc,
)

GRAPH_SCHEMA = "datafabric.graph.bundle.v1"
GRAPH_SOURCES = {"reviewed_graph", "primekg_graph"}
DERIVED_SOURCES = GRAPH_SOURCES | {"legacy_documents"}


def identity_key(value):
    return value.namespace, value.id


def clean_identity(value):
    if not value or "|" in value or any(ord(char) < 33 for char in value):
        raise ValueError("身份命名空间/ID不得为空或含空白、控制符和竖线")
    return value


class PrimeKGLookup(Model):
    """当前图里的精确名称候选，不代替唐的身份消歧。"""

    name: str = Field(min_length=1, max_length=120)
    node_type: Literal["drug", "disease", "gene/protein"]
    limit: int = Field(default=10, ge=1, le=20, strict=True)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("名称不得全为空白或含控制字符")
        return value.strip()


class PrimeKGQuery(Model):
    """仅允许当前图的一跳查询，参数会随发布包保存。"""

    node_type: Literal["drug", "disease", "gene/protein"]
    node_source: str = Field(min_length=1, max_length=152)
    seed_id: str = Field(min_length=1, max_length=400)
    relation_types: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=16
    )
    neighbor_term: str = Field(default="", max_length=120)
    limit: int = Field(default=20, ge=1, le=200, strict=True)

    @field_validator("node_source", "seed_id")
    @classmethod
    def valid_identity(cls, value):
        return clean_identity(value)

    @field_validator("relation_types")
    @classmethod
    def normalize_relations(cls, values):
        return sorted(set(values))

    def scope(self):
        return "PrimeKG bounded current query: " + canonical(self.model_dump())


class GraphIdentity(Model):
    namespace: str = Field(min_length=1, max_length=160)
    id: str = Field(min_length=1, max_length=400)

    @field_validator("namespace", "id")
    @classmethod
    def valid_identity(cls, value):
        return clean_identity(value)


class GraphReview(Model):
    status: str = Field(min_length=1, max_length=80)
    reviewed_by: str | None = Field(default=None, max_length=200)
    reviewed_at: datetime | None = None

    @field_validator("reviewed_at")
    @classmethod
    def aware_review(cls, value):
        return utc(value) if value is not None else None


class GraphDocumentRef(Model):
    source_id: str = Field(min_length=1, max_length=80)
    source_record_id: str = Field(min_length=1, max_length=400)
    version_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin_doc_uuid: str | None = Field(default=None, max_length=400)


class GraphLocator(Model):
    basis: Literal["clean_text"] = "clean_text"
    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("证据字符范围必须非空")
        return self


class GraphEvidence(GraphDocumentRef):
    quote: str = Field(min_length=1, max_length=8000)
    locator: GraphLocator


class GraphFieldEvidence(GraphDocumentRef):
    json_pointer: str = Field(min_length=1, max_length=1000)
    value: Any

    @field_validator("json_pointer")
    @classmethod
    def pointer(cls, value):
        if not value.startswith("/"):
            raise ValueError("字段出处须为原件JSON pointer")
        return value


class GraphIdentityRule(Model):
    method_version: Literal["clinicaltrials.native-id.v1"] = "clinicaltrials.native-id.v1"
    namespace: Literal["clinicaltrials.NCT"] = "clinicaltrials.NCT"
    value: str = Field(pattern="^" + NCT_ID + "$")
    basis: GraphFieldEvidence


class GraphNode(GraphIdentity):
    entity_type: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    external_ids: dict[str, Any] = Field(default_factory=dict)
    identity_status: Literal["confirmed", "rule_confirmed", "unresolved", "background"]
    identity_review_basis: str | None = Field(default=None, max_length=2000)
    identity_rule: GraphIdentityRule | None = None
    review: GraphReview
    mention_ids: list[str] = Field(default_factory=list, max_length=1000)
    evidence: list[GraphEvidence] = Field(default_factory=list, max_length=100)
    field_evidence: list[GraphFieldEvidence] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def identity_has_basis(self):
        if self.identity_status == "confirmed" and (
            self.review.status != "approved" or not self.identity_review_basis
        ):
            raise ValueError("已确认身份须有源系统人工审核及身份依据")
        if self.identity_status == "rule_confirmed":
            rule = self.identity_rule
            if (rule is None or self.namespace != rule.namespace or self.id != rule.value
                    or rule.basis.value != rule.value or self.review.status != "rule_confirmed"):
                raise ValueError("规则身份须绑定原生编号、规则版本与精确字段出处")
        elif self.identity_rule is not None:
            raise ValueError("原生编号规则只能用于rule_confirmed身份")
        return self


class GraphEdge(Model):
    id: str = Field(min_length=1, max_length=400)
    source: GraphIdentity
    target: GraphIdentity
    relation_type: str = Field(min_length=1, max_length=120)
    directed: Literal[True] = True
    relation_props: dict[str, Any] = Field(default_factory=dict)
    valid_from: TimeValue | None = None
    valid_to: TimeValue | None = None
    review: GraphReview
    binding_status: Literal["bound", "unbound", "background"]
    evidence: list[GraphEvidence] = Field(default_factory=list, max_length=100)
    field_evidence: list[GraphFieldEvidence] = Field(default_factory=list, max_length=100)
    source_doc_uuid: str | None = Field(default=None, max_length=400)
    unbound_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def bound_has_evidence(self):
        if self.binding_status == "bound" and (
            not (self.evidence or self.field_evidence)
            or self.review.status not in {"approved", "auto_approved", "pending", "rule_confirmed"}
        ):
            raise ValueError("已绑定关系须有审核状态和精确原文引用")
        if self.binding_status == "background" and (self.evidence or self.field_evidence):
            raise ValueError("背景关系不得伪造文档证据")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from.lower_bound() > self.valid_to.upper_bound()
        ):
            raise ValueError("关系有效时间范围颠倒")
        return self


class GraphRedirect(Model):
    source: GraphIdentity
    target: GraphIdentity
    merge_log_id: str = Field(min_length=1, max_length=200)
    reviewed_by: str | None = Field(default=None, max_length=200)
    merged_at: datetime | None = None

    @field_validator("merged_at")
    @classmethod
    def aware_merge(cls, value):
        return utc(value) if value is not None else None


class GraphBundle(Model):
    schema_version: Literal["datafabric.graph.bundle.v1"] = GRAPH_SCHEMA
    graph_kind: Literal["reviewed", "background"]
    namespace: str = Field(min_length=1, max_length=160)
    is_demo: bool
    title: str = Field(min_length=1, max_length=1000)
    origin: Literal[
        "legacy_reviewed_export", "primekg_current_capture", "explicit_demo", "source_field_projection"
    ]
    scope_description: str = Field(min_length=1, max_length=4000)
    selected_document_uuids: list[str] = Field(default_factory=list, max_length=100)
    scope_record_ids: list[str] = Field(default_factory=list, max_length=200)
    scope_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scope_record_count: int | None = Field(default=None, ge=0, le=20000)
    batch_offset: int = Field(default=0, ge=0, le=20000)
    next_document_offset: int | None = Field(default=None, ge=0)
    dependency_source_ids: list[str] = Field(default_factory=list, max_length=100)
    document_refs: list[GraphDocumentRef] = Field(default_factory=list, max_length=100)
    selection_truncated: bool = False
    nodes: list[GraphNode] = Field(max_length=1000)
    edges: list[GraphEdge] = Field(max_length=3000)
    redirects: list[GraphRedirect] = Field(default_factory=list, max_length=1000)
    limitations: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("namespace")
    @classmethod
    def valid_namespace(cls, value):
        return clean_identity(value)

    @model_validator(mode="after")
    def consistent(self):
        keys = [identity_key(node) for node in self.nodes]
        node_keys = set(keys)
        if len(keys) != len(node_keys) or len({edge.id for edge in self.edges}) != len(
            self.edges
        ):
            raise ValueError("图节点身份或关系ID重复")
        if any(
            identity_key(edge.source) not in node_keys
            or identity_key(edge.target) not in node_keys
            for edge in self.edges
        ):
            raise ValueError("关系端点必须属于同一冻结图包")
        if any(
            identity_key(item.target) not in node_keys
            or identity_key(item.source) == identity_key(item.target)
            for item in self.redirects
        ):
            raise ValueError("身份重定向须指向本包节点且不能指向自身")
        if len(set(self.dependency_source_ids)) != len(self.dependency_source_ids):
            raise ValueError("来源依赖重复")
        if self.origin == "explicit_demo" and not self.is_demo:
            raise ValueError("教学图不得取消demo标记")
        if self.origin == "primekg_current_capture" and self.graph_kind != "background":
            raise ValueError("当前PrimeKG只能作背景图")
        if self.graph_kind == "background" and (
            self.document_refs
            or self.dependency_source_ids
            or any(
                node.identity_status != "background" or node.evidence or node.field_evidence
                for node in self.nodes
            )
            or any(
                edge.binding_status != "background" or edge.evidence or edge.field_evidence
                for edge in self.edges
            )
        ):
            raise ValueError("背景图不得混入已确认身份或伪造文档证据")
        return self


class GraphRef(GraphDocumentRef):
    is_demo: bool
    first_seen_at: datetime


class GraphCoverage(Model):
    scope: Literal["selected_published_subgraph"] = "selected_published_subgraph"
    truncated: bool
    selection_truncated: bool
    stop_reason: str
    returned_nodes: int
    returned_edges: int
    max_depth: int


class GraphNeighborhood(Model):
    schema_version: Literal["datafabric.graph.neighborhood.v1"] = (
        "datafabric.graph.neighborhood.v1"
    )
    graph_ref: GraphRef
    namespace: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    redirects: list[GraphRedirect]
    seeds_found: list[GraphIdentity]
    seeds_missing: list[GraphIdentity]
    coverage: GraphCoverage
    limitations: list[str]


class LegacyDocument(Model):
    schema_version: Literal["datafabric.legacy.document.v1"] = (
        "datafabric.legacy.document.v1"
    )
    source_doc_uuid: str = Field(min_length=1, max_length=400)
    source_site: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=10000)
    source_url: str | None = Field(default=None, max_length=4000)
    original_raw_content: str = Field(min_length=1)
    original_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publish_date_raw: str | None = Field(default=None, max_length=200)
    original_created_at: str | None = Field(default=None, max_length=200)
    original_updated_at: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def raw_hash(self):
        if sha256(self.original_raw_content) != self.original_raw_sha256:
            raise ValueError("旧文档原文SHA不一致")
        if self.source_site in DERIVED_SOURCES:
            raise ValueError("旧文档必须指明原始站点，不能再套派生来源")
        return self


def document_native_id(document):
    return "legacy:" + document.source_site + ":" + document.source_doc_uuid


def neighborhood(
    bundle,
    graph_ref,
    seeds,
    *,
    relation_types=(),
    direction="both",
    max_depth=1,
    max_nodes=50,
    max_edges=100,
    review_scope=None,
):
    """只遍历已发布的有限包；保留边方向，不补相同名字的节点或静默跟随redirect。"""
    if (
        direction not in {"out", "in", "both"}
        or not 0 <= max_depth <= 2
        or not 1 <= max_nodes <= 100
        or not 1 <= max_edges <= 200
    ):
        raise HistoryError("graph_query_budget", "图查询超过方向、深度或数量预算")
    if not seeds or len(seeds) > 20 or len(relation_types) > 16:
        raise HistoryError("graph_query_budget", "须给1至20个明确身份及至多16种关系")
    if len({identity_key(seed) for seed in seeds}) != len(seeds):
        raise HistoryError("duplicate_graph_seed", "查询身份重复")
    if review_scope is not None:
        from biz_rules.graph_tools import select_bundle
        bundle = select_bundle(bundle, review_scope)
    nodes = {identity_key(node): node for node in bundle.nodes}
    found = [seed for seed in seeds if identity_key(seed) in nodes]
    missing = [seed for seed in seeds if identity_key(seed) not in nodes]
    visited, picked, picked_ids = {}, [], set()
    boundary = set()
    queue = deque((identity_key(seed), 0) for seed in found)
    adjacency = {}
    for edge in bundle.edges:
        if relation_types and edge.relation_type not in relation_types:
            continue
        if direction in {"out", "both"}:
            adjacency.setdefault(identity_key(edge.source), []).append(
                (edge, identity_key(edge.target))
            )
        if direction in {"in", "both"}:
            adjacency.setdefault(identity_key(edge.target), []).append(
                (edge, identity_key(edge.source))
            )
    for values in adjacency.values():
        values.sort(key=lambda pair: pair[0].id)
    reason = None
    while queue:
        key, depth = queue.popleft()
        if key in visited:
            continue
        if len(visited) >= max_nodes:
            reason = "node_budget"
            break
        visited[key] = nodes[key]
        if depth >= max_depth:
            boundary.add(key)
            continue
        for edge, other in adjacency.get(key, ()):
            if edge.id in picked_ids:
                continue
            if (
                other not in visited
                and all(item[0] != other for item in queue)
                and len(visited) + len(queue) >= max_nodes
            ):
                reason = "node_budget"
                continue
            if len(picked) >= max_edges:
                reason = "edge_budget"
                break
            picked.append(edge)
            picked_ids.add(edge.id)
            if other not in visited:
                queue.append((other, depth + 1))
    if not reason and any(
        edge.id not in picked_ids
        for key in boundary
        for edge, _ in adjacency.get(key, ())
    ):
        reason = "depth_budget"
    # 边端点必须全部在实际返回节点内；预算被截断时不留下悬空引用。
    picked = [
        edge
        for edge in picked
        if identity_key(edge.source) in visited and identity_key(edge.target) in visited
    ]
    redirects = [
        item for item in bundle.redirects if identity_key(item.target) in visited
    ]
    limitations = [
        *bundle.limitations,
        "范围仅为这个固定发布包；未自动合并身份，未证明关系因果或专业有效性",
    ]
    if missing:
        limitations.append("部分查询身份未出现在所选图包，不代表全图或真实世界不存在")
    return GraphNeighborhood(
        graph_ref=graph_ref,
        namespace=bundle.namespace,
        nodes=list(visited.values()),
        edges=picked,
        redirects=redirects,
        seeds_found=found,
        seeds_missing=missing,
        coverage=GraphCoverage(
            truncated=bool(reason) or bundle.selection_truncated,
            selection_truncated=bundle.selection_truncated,
            stop_reason=reason
            or (
                "source_selection_limit"
                if bundle.selection_truncated
                else "end_of_published_subgraph"
            ),
            returned_nodes=len(visited),
            returned_edges=len(picked),
            max_depth=max_depth,
        ),
        limitations=limitations,
    )


def primekg_bundle(observation):
    """接收只读捕获，不连接Neo4j；保留原生端点/类型，不把当前PPI解释成新证据。"""
    if (
        observation.get("source") != "primekg_current_projection"
        or observation.get("is_demo") is not False
    ):
        raise HistoryError(
            "invalid_primekg_capture", "真实PrimeKG捕获须标明当前来源及非demo"
        )
    utc(datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00")))
    rows = observation.get("rows")
    if (
        not isinstance(rows, list)
        or len(rows) > 200
        or (not rows and not isinstance(observation.get("seed_found"), bool))
    ):
        raise HistoryError(
            "invalid_primekg_capture",
            "PrimeKG捕获须有明确空/非空结果及至多200条有界关系",
        )
    nodes, edges = {}, {}
    if not rows and observation.get("seed_found") is True:
        original = observation["seed_node"]
        seed = GraphNode(
            namespace="primekg." + clean_identity(str(original["source"])),
            id=clean_identity(str(original["id"])),
            entity_type=original["type"],
            name=original["name"],
            external_ids={original["source"]: str(original["id"])},
            identity_status="background",
            identity_review_basis="primekg_current_source_identity",
            review=GraphReview(status="background"),
        )
        nodes[identity_key(seed)] = seed
    for row in rows:
        pair = []
        for key in ("from_node", "to_node"):
            original = row[key]
            node = GraphNode(
                namespace="primekg." + clean_identity(str(original["source"])),
                id=clean_identity(str(original["id"])),
                entity_type=original["type"],
                name=original["name"],
                aliases=[],
                external_ids={original["source"]: str(original["id"])},
                identity_status="background",
                identity_review_basis="primekg_current_source_identity",
                review=GraphReview(status="background"),
                mention_ids=[],
                evidence=[],
            )
            old = nodes.get(identity_key(node))
            if old is not None and old != node:
                raise HistoryError(
                    "ambiguous_primekg_identity", "同一PrimeKG身份在捕获中有冲突"
                )
            nodes[identity_key(node)] = node
            pair.append(GraphIdentity(namespace=node.namespace, id=node.id))
        props = row.get("properties") or {}
        edge_id = sha256(
            canonical(
                {
                    "source": pair[0].model_dump(),
                    "target": pair[1].model_dump(),
                    "type": row["relation_type"],
                    "properties": props,
                }
            )
        )
        edges[edge_id] = GraphEdge(
            id=edge_id,
            source=pair[0],
            target=pair[1],
            relation_type=row["relation_type"],
            relation_props=props,
            review=GraphReview(status="background"),
            binding_status="background",
        )
    seed = observation.get("seed") or {}
    return GraphBundle(
        graph_kind="background",
        namespace="primekg",
        is_demo=False,
        title="PrimeKG 当前有界背景邻域",
        origin="primekg_current_capture",
        scope_description=str(
            observation.get("query") or "explicit bounded current capture"
        ),
        selected_document_uuids=[],
        scope_record_ids=[
            str(seed.get("node_source", "")) + ":" + str(seed.get("primekg_id", ""))
        ],
        dependency_source_ids=[],
        document_refs=[],
        selection_truncated=observation.get("has_more") is True,
        nodes=list(nodes.values()),
        edges=list(edges.values()),
        limitations=[
            "捕获时的PrimeKG背景；可回看本地first_seen后已收到的版本，不证明更早世界状态或专业证据",
            "原捕获时间：" + observation["observed_at"],
        ],
    )


def project_graph_record(source_id, item, data):
    """只有这三个固定派生来源可使用图桥结构；保留旧四种原件投影不变。"""
    from biz_rules.history_sources import _facts

    if source_id == "legacy_documents":
        if data.get("schema_version") == "datafabric.collected.document.v1":
            from biz_rules.collected_document import project_collected

            return project_collected(item, data)
        document = LegacyDocument.model_validate(data)
        if (
            item.record_kind != "document"
            or item.source_record_id != document_native_id(document)
        ):
            raise HistoryError(
                "legacy_document_identity", "旧文档UUID/原站点与采用ID不一致"
            )
        fields = {
            "origin_doc_uuid": "/source_doc_uuid",
            "source_site": "/source_site",
            "original_raw_sha256": "/original_raw_sha256",
            "source_url": "/source_url",
            "publish_date_raw": "/publish_date_raw",
        }
        facts, locators = _facts(data, fields)
        return {
            "parser_version": "datafabric.legacy.document.projection.v0.2",
            "record_kind": "document",
            "title": document.title,
            "source_url": document.source_url,
            "clean_text": document.original_raw_content,
            "facts": facts,
            "field_locators": locators,
            "event_time": None,
            "source_published_at": None,
            "source_updated_at": None,
        }
    bundle = GraphBundle.model_validate(data)
    expected_kind = "background" if source_id == "primekg_graph" else "reviewed"
    if item.record_kind != "graph" or bundle.graph_kind != expected_kind:
        raise HistoryError("graph_kind_mismatch", "来源、图类型和原件类型不一致")
    fields = (
        "graph_kind",
        "namespace",
        "origin",
        "scope_description",
        "dependency_source_ids",
        "selection_truncated",
    )
    facts, locators = _facts(data, {key: "/" + key for key in fields})
    return {
        "parser_version": "datafabric.graph.projection.v0.2",
        "record_kind": "graph",
        "title": bundle.title,
        "source_url": None,
        "clean_text": item.raw_content,
        "facts": facts,
        "field_locators": locators,
        "event_time": None,
        "source_published_at": None,
        "source_updated_at": None,
    }


class GraphResolutionQuery(Model):
    name: str | None = Field(default=None, min_length=1, max_length=1000)
    external_namespace: str | None = Field(default=None, min_length=1, max_length=160)
    external_id: str | None = Field(default=None, min_length=1, max_length=400)

    @model_validator(mode="after")
    def one_query(self):
        if bool(self.name) == bool(self.external_namespace or self.external_id):
            raise ValueError("请选择精确名称或完整外部ID")
        if not self.name and not (self.external_namespace and self.external_id):
            raise ValueError("外部ID须同时给命名空间和值")
        return self


class GraphResolution(Model):
    schema_version: Literal["datafabric.graph.resolution.v1"] = (
        "datafabric.graph.resolution.v1"
    )
    graph_ref: GraphRef
    query: GraphResolutionQuery
    status: Literal["resolved", "ambiguous", "not_found"]
    candidates: list[GraphNode]
    truncated: bool
    limitations: list[str]


def resolve(
    bundle, graph_ref, *, name=None, external_namespace=None, external_id=None, limit=20,
    review_scope=None,
):
    if not 1 <= limit <= 20:
        raise HistoryError("graph_query_budget", "身份查询最多返回20个候选")
    try:
        query = GraphResolutionQuery(
            name=name, external_namespace=external_namespace, external_id=external_id
        )
    except ValueError:
        raise HistoryError(
            "invalid_graph_resolution", "须给精确名称或完整外部命名空间/ID"
        ) from None
    if review_scope is not None:
        from biz_rules.graph_tools import select_bundle
        bundle = select_bundle(bundle, review_scope)
    matches = []
    for node in bundle.nodes:
        if name is not None:
            matched = name.casefold() in {
                value.casefold() for value in [node.name, *node.aliases]
            }
        else:
            values = node.external_ids.get(external_namespace)
            matched = (
                external_id in values
                if isinstance(values, list)
                else values == external_id
            )
        if matched:
            matches.append(node)
    matches.sort(key=identity_key)
    return GraphResolution(
        graph_ref=graph_ref,
        query=query,
        status="ambiguous"
        if len(matches) > 1
        else "resolved"
        if matches
        else "not_found",
        candidates=matches[:limit],
        truncated=len(matches) > limit,
        limitations=[
            *bundle.limitations,
            "仅固定包中的精确名称/获准别名/外部ID匹配；同名不代表同一实体，不自动选择候选",
        ],
    )
