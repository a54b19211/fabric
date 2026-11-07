"""明确来源字段的图投影；不从名称推断真实身份或机制关系。"""

from processing.identifiers import NCT_ID
import re

from biz_rules.history_models import HistoryError, HistoryRecord, canonical, sha256
from biz_rules.history_sources import parse_json
from biz_rules.native_extraction import TRIAL_ATTRIBUTE_PATHS, clinical_trial_attributes, field_value
from biz_rules.reviewed_graph import (
    GraphBundle, GraphDocumentRef, GraphEdge, GraphFieldEvidence, GraphIdentity,
    GraphIdentityRule, GraphNode, GraphReview,
)

METHOD = "clinicaltrials.fields.v2"
LEGACY_METHOD = "clinicaltrials.fields.v1"
NATIVE_PATH = "/protocolSection/identificationModule/nctId"
RELATIONS = {
    "REGISTERED_CONDITION": ("conditionsModule/conditions", "Indication", "condition"),
    "REGISTERED_INTERVENTION": ("armsInterventionsModule/interventions", "Intervention", "intervention"),
    "REGISTERED_SPONSOR": ("sponsorCollaboratorsModule/leadSponsor", "Organization", "sponsor"),
    "REGISTERED_COLLABORATOR": ("sponsorCollaboratorsModule/collaborators", "Organization", "collaborator"),
}


def _key(ref):
    return (ref.source_id, ref.source_record_id, ref.version_id, ref.raw_sha256, ref.origin_doc_uuid)


def _pointer(data, pointer):
    value = data
    for token in pointer.split("/")[1:]:
        if re.search(r"~(?![01])", token):
            raise ValueError("无效JSON pointer")
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _native(record):
    if record["source_id"] != "clinicaltrials":
        raise ValueError("本规则仅支持ClinicalTrials原生原件")
    if sha256(record["raw_content"]) != record["raw_sha256"]:
        raise ValueError("原件SHA不一致")
    data = parse_json(record["raw_content"])
    identity = _pointer(data, NATIVE_PATH)
    if not isinstance(identity, str) or not re.fullmatch(NCT_ID, identity):
        raise ValueError("缺少原生NCT登记号")
    if record["source_record_id"] != identity:
        raise ValueError("原生登记号与采用记录身份矛盾")
    return data, identity


def _relation(pointer, *, legacy=False):
    for kind, (path, entity_type, suffix) in RELATIONS.items():
        if legacy and kind == "REGISTERED_COLLABORATOR":
            continue
        stem = "/protocolSection/" + path
        pattern = stem + (r"/[0-9]+" if suffix == "condition" else r"/[0-9]+/name"
                          if suffix in {"intervention", "collaborator"} else "/name")
        if re.fullmatch(pattern, pointer):
            return kind, entity_type, suffix
    raise ValueError("字段不属于已定义的登记关系")


def _bound(documents, ref):
    """The NCT identity of the frozen original that `ref` points into, after checking the cited value."""
    record = documents.get(_key(ref))
    if record is None:
        raise ValueError("字段引用不属于所选原件清单")
    data, identity = _native(record)
    if canonical(_pointer(data, ref.json_pointer)) != canonical(ref.value):
        raise ValueError("字段值与冻结原件不一致")
    return identity


def _is_legacy_contract(bundle, allow_legacy):
    """Fixed History reads may validate the exact v1 contract; everything else must be v2."""
    methods = {edge.relation_props.get("method_version") for edge in bundle.edges if edge.field_evidence}
    legacy = (allow_legacy and bundle.origin == "source_field_projection"
              and (methods == {LEGACY_METHOD} or
                   (not bundle.edges and all(not node.properties and not node.field_evidence
                                            for node in bundle.nodes))))
    if methods - ({LEGACY_METHOD} if legacy else {METHOD}):
        raise ValueError("未知或混合的原生字段规则版本")
    if legacy and any(node.properties or (node.identity_status == "rule_confirmed" and node.field_evidence)
                      for node in bundle.nodes):
        raise ValueError("v1冻结图不含新增试验属性或字段证据")
    return legacy


def _check_projection_scope(bundle, documents):
    """A source-field projection names its originals and its registered range explicitly."""
    refs = bundle.document_refs
    if (bundle.namespace != "clinicaltrials.source_fields" or not refs or not bundle.nodes
            or len(refs) > 20 or bundle.selected_document_uuids or bundle.redirects
            or set(bundle.dependency_source_ids) != {"clinicaltrials"}):
        raise ValueError("原生字段图须有明确原件清单与登记范围")
    if (any(ref.source_id != "clinicaltrials" or ref.origin_doc_uuid is not None for ref in refs)
            or len({ref.source_record_id for ref in refs}) != len(refs)
            or sorted(bundle.scope_record_ids) != sorted(ref.source_record_id for ref in refs)):
        raise ValueError("原生字段图须有明确原件清单与登记范围")
    for ref in refs:
        _native(documents[_key(ref)])


def _check_projection_objects(bundle):
    """... and carries only rule-confirmed objects that are bound to source fields."""
    if any(node.review.status != "rule_confirmed"
           or node.identity_status not in {"rule_confirmed", "unresolved"}
           or (node.identity_status == "unresolved" and not node.field_evidence)
           or node.evidence or node.mention_ids for node in bundle.nodes):
        raise ValueError("原生字段图不得夹入人工确认或无字段出处的节点")
    if any(edge.review.status != "rule_confirmed" or not edge.field_evidence
           or edge.evidence for edge in bundle.edges):
        raise ValueError("原生字段图不得夹入非来源字段关系")


def _check_node_field(node, ref, documents, legacy):
    """One field-evidence ref of a node: a trial attribute of its own NCT, or the value of a registered child."""
    identity = _bound(documents, ref)
    if node.identity_status == "rule_confirmed" and not legacy:
        if (identity != node.id or ref.json_pointer not in TRIAL_ATTRIBUTE_PATHS.values()
                or _key(ref) != _key(node.identity_rule.basis)):
            raise ValueError("试验属性出处与编号原件不一致")
        return
    _kind, expected_type, suffix = _relation(ref.json_pointer, legacy=legacy)
    if (node.review.status != "rule_confirmed" or node.identity_status != "unresolved"
            or node.entity_type != expected_type or node.name != ref.value
            or node.aliases or node.external_ids or node.properties
            or node.namespace != "clinicaltrials." + identity + "." + suffix
            or node.id != sha256(ref.value)):
        raise ValueError("登记字段节点的类型/身份与原件不一致")


def _check_trial_identity(node, documents, legacy):
    """The NCT rule of a trial node, and (v2) that its attributes keep every original field and date precision."""
    rule = node.identity_rule
    identity = _bound(documents, rule.basis)
    if (rule.basis.json_pointer != NATIVE_PATH or rule.value != identity
            or node.entity_type != "ClinicalTrial" or node.name != identity
            or node.aliases
            or node.external_ids != {"NCT": identity}):
        raise ValueError("原生编号规则不能升级其他名称或类型")
    if legacy:
        return
    data, _identity = _native(documents[_key(rule.basis)])
    expected = clinical_trial_attributes(data)
    missing = object()
    expected_fields = {path: field_value(data, path, missing) for path in TRIAL_ATTRIBUTE_PATHS.values()}
    expected_fields = {path: value for path, value in expected_fields.items() if value is not missing}
    if (canonical(node.properties) != canonical(expected)
            or len(node.field_evidence) != len(expected_fields)
            or {ref.json_pointer: ref.value for ref in node.field_evidence} != expected_fields):
        raise ValueError("试验属性未完整保留原件字段或日期精度")


def _check_node(node, documents, legacy):
    for ref in node.field_evidence:
        _check_node_field(node, ref, documents, legacy)
    if node.identity_status == "rule_confirmed":
        _check_trial_identity(node, documents, legacy)
    elif node.review.status == "rule_confirmed" and not node.field_evidence:
        raise ValueError("规则节点缺少字段出处")


def _check_edge_field(edge, ref, nodes, documents, legacy):
    """One field-evidence ref of an edge: trial -> registered child, both ends bound to the same original."""
    identity = _bound(documents, ref)
    kind, entity_type, suffix = _relation(ref.json_pointer, legacy=legacy)
    source = nodes[(edge.source.namespace, edge.source.id)]
    target = nodes[(edge.target.namespace, edge.target.id)]
    if (edge.relation_type != kind or edge.review.status != "rule_confirmed"
            or edge.binding_status != "bound"
            or edge.relation_props != {"method_version": LEGACY_METHOD if legacy else METHOD}
            or source.namespace != "clinicaltrials.NCT" or source.id != identity
            or source.identity_status != "rule_confirmed"
            or _key(source.identity_rule.basis) != _key(ref)
            or target.entity_type != entity_type or target.name != ref.value
            or target.namespace != "clinicaltrials." + identity + "." + suffix
            or target.id != sha256(ref.value) or ref not in target.field_evidence
            or edge.valid_from is not None or edge.valid_to is not None):
        raise ValueError("登记关系不符合来源字段规则")


def validate_field_bindings(bundle, documents, *, allow_legacy=False):
    """New publications require v2; fixed History reads may validate the exact v1 contract."""
    nodes = {(node.namespace, node.id): node for node in bundle.nodes}
    try:
        legacy = _is_legacy_contract(bundle, allow_legacy)
        if bundle.origin == "source_field_projection":
            _check_projection_scope(bundle, documents)
            _check_projection_objects(bundle)
        for node in bundle.nodes:
            _check_node(node, documents, legacy)
        for edge in bundle.edges:
            for ref in edge.field_evidence:
                _check_edge_field(edge, ref, nodes, documents, legacy)
            if edge.review.status == "rule_confirmed" and not edge.field_evidence:
                raise ValueError("规则关系缺少字段出处")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise HistoryError("graph_field_mismatch", "图身份/关系字段与冻结原件规则不一致") from None


def build_native_graph(records, *, max_nodes=100, max_edges=200):
    """固定ClinicalTrials版本→登记对象及其条件/干预/申办方；不声称药物谱系。"""
    records = [HistoryRecord.model_validate(record) for record in records]
    if (not records or len(records) > 20 or len({record.is_demo for record in records}) != 1
            or not 1 <= max_nodes <= 100 or not 1 <= max_edges <= 200
            or len({(record.source_id, record.source_record_id) for record in records}) != len(records)):
        raise HistoryError("native_graph_budget", "须给1至20个同空间、不重复的固定登记版本及有效预算")
    records.sort(key=lambda record: (record.source_id, record.source_record_id))
    nodes, edges, refs, documents = {}, [], [], {}
    truncated = False
    for record in records:
        raw = record.model_dump(mode="json")
        try:
            data, identity = _native(raw)
        except (KeyError, IndexError, TypeError, ValueError):
            raise HistoryError("native_graph_identity", "登记原件缺少可核对的原生编号") from None
        ref = GraphDocumentRef(source_id=record.source_id, source_record_id=record.source_record_id,
                               version_id=record.version_id, raw_sha256=record.raw_sha256)
        refs.append(ref)
        documents[_key(ref)] = raw
        if len(nodes) >= max_nodes:
            truncated = True
            continue
        basis = GraphFieldEvidence(**ref.model_dump(), json_pointer=NATIVE_PATH, value=identity)
        try:
            properties = clinical_trial_attributes(data)
        except (ValueError, TypeError, AttributeError):
            raise HistoryError("native_graph_fields", "试验属性不符合原始登记字段类型") from None
        missing = object()
        fields = [GraphFieldEvidence(**ref.model_dump(), json_pointer=pointer, value=value)
                  for pointer in TRIAL_ATTRIBUTE_PATHS.values()
                  if (value := field_value(data, pointer, missing)) is not missing]
        trial = GraphNode(namespace="clinicaltrials.NCT", id=identity, name=identity,
                          entity_type="ClinicalTrial", external_ids={"NCT": identity},
                          identity_status="rule_confirmed", identity_rule=GraphIdentityRule(value=identity, basis=basis),
                          properties=properties, field_evidence=fields,
                          identity_review_basis="原生NCT字段逐值核对；不是人工审核或药物项目谱系",
                          review=GraphReview(status="rule_confirmed"))
        nodes[(trial.namespace, trial.id)] = trial
        protocol = data["protocolSection"]
        values = []
        for index, value in enumerate(protocol.get("conditionsModule", {}).get("conditions", [])):
            values.append(("REGISTERED_CONDITION", f"/protocolSection/conditionsModule/conditions/{index}", value))
        for index, item in enumerate(protocol.get("armsInterventionsModule", {}).get("interventions", [])):
            values.append(("REGISTERED_INTERVENTION", f"/protocolSection/armsInterventionsModule/interventions/{index}/name", item.get("name")))
        sponsor = protocol.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name")
        if sponsor:
            values.append(("REGISTERED_SPONSOR", "/protocolSection/sponsorCollaboratorsModule/leadSponsor/name", sponsor))
        for index, item in enumerate(protocol.get("sponsorCollaboratorsModule", {}).get("collaborators", [])):
            values.append(("REGISTERED_COLLABORATOR",
                           f"/protocolSection/sponsorCollaboratorsModule/collaborators/{index}/name", item.get("name")))
        for kind, pointer, value in values:
            if not isinstance(value, str) or not value or len(value) > 1000:
                truncated = True
                continue
            _kind, entity_type, suffix = _relation(pointer)
            child_key = ("clinicaltrials." + identity + "." + suffix, sha256(value))
            if (child_key not in nodes and len(nodes) >= max_nodes) or len(edges) >= max_edges:
                truncated = True
                continue
            field = GraphFieldEvidence(**ref.model_dump(), json_pointer=pointer, value=value)
            if child_key not in nodes:
                nodes[child_key] = GraphNode(namespace=child_key[0], id=child_key[1], entity_type=entity_type,
                                            name=value, identity_status="unresolved",
                                            review=GraphReview(status="rule_confirmed"), field_evidence=[field])
            elif field not in nodes[child_key].field_evidence:
                if len(nodes[child_key].field_evidence) >= 100:
                    truncated = True
                    continue
                nodes[child_key].field_evidence.append(field)
            edges.append(GraphEdge(id="native-field:" + sha256(canonical({"ref": ref.model_dump(), "pointer": pointer})),
                                   source=GraphIdentity(namespace=trial.namespace, id=trial.id),
                                   target=GraphIdentity(namespace=child_key[0], id=child_key[1]), relation_type=kind,
                                   relation_props={"method_version": METHOD}, review=GraphReview(status="rule_confirmed"),
                                   binding_status="bound", field_evidence=[field]))
    bundle = GraphBundle(graph_kind="reviewed", namespace="clinicaltrials.source_fields", is_demo=records[0].is_demo,
                         title="固定登记版本的对象与来源字段关系", origin="source_field_projection",
                         scope_description="明确登记原件版本的字段投影；不是全行业项目队列或药物项目谱系",
                         scope_record_ids=[record.source_record_id for record in records],
                         dependency_source_ids=["clinicaltrials"], document_refs=refs,
                         selection_truncated=truncated, nodes=list(nodes.values()), edges=edges,
                         limitations=["NCT只确认试验登记对象；条件、干预和申办方按本登记范围保留，未做跨项目名称合并",
                                      "关系表示本版本登记字段，不证明机制、疗效或专业审核；rule_confirmed与人工approved不同",
                                      "关系valid_from/valid_to未知，不用抓取或文档日期补造；历史读取受固定原件首次可见时间约束"])
    validate_field_bindings(bundle, documents)
    return bundle
