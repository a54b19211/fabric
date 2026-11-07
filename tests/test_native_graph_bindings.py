"""validate_field_bindings: one forged or inconsistent bundle per rule, and the exact v1 contract for fixed reads.

Every case is a small change to a valid native graph; `REJECTED` are the changes the validator must refuse with
graph_field_mismatch, the others are changes it has to accept (they are not part of what the rules constrain)."""

import pytest
from test_graph_tools import context, record

from biz_rules.history_models import HistoryError, TimeValue, sha256
from biz_rules.native_graph import LEGACY_METHOD, build_native_graph, validate_field_bindings


def _graph():
    rec = record()
    graph = build_native_graph([rec])
    trial = next(n for n in graph.nodes if n.entity_type == "ClinicalTrial")
    children = [n for n in graph.nodes if n is not trial]
    return rec, graph, trial, children


def _edge_of(graph, kind):
    return next(e for e in graph.edges if e.relation_type == kind)


def _node_of(graph, name):
    return next(n for n in graph.nodes if n.name == name)


def _set(obj, **fields):
    for key, value in fields.items():
        setattr(obj, key, value)


# name -> mutation(rec, graph, trial, documents)
MUTATIONS = {
    "bundle_namespace": lambda r, g, t, d: _set(g, namespace="other"),
    "bundle_no_document_refs": lambda r, g, t, d: _set(g, document_refs=[]),
    "bundle_scope_mismatch": lambda r, g, t, d: _set(g, scope_record_ids=["NCT00000002"]),
    "bundle_dependency_source": lambda r, g, t, d: _set(g, dependency_source_ids=["clinicaltrials", "x"]),
    "bundle_doc_origin_uuid": lambda r, g, t, d: _set(g.document_refs[0], origin_doc_uuid="u"),
    "bundle_no_nodes": lambda r, g, t, d: _set(g, nodes=[]),
    "doc_missing": lambda r, g, t, d: d.clear(),
    "doc_wrong_sha": lambda r, g, t, d: [v.update(raw_sha256="0" * 64) for v in d.values()],
    "doc_other_source": lambda r, g, t, d: [v.update(source_id="other") for v in d.values()],
    "doc_identity_mismatch": lambda r, g, t, d: [v.update(source_record_id="NCT00000009") for v in d.values()],
    "trial_review_pending": lambda r, g, t, d: _set(t.review, status="pending"),
    "trial_alias": lambda r, g, t, d: t.aliases.append("alias"),
    "trial_name": lambda r, g, t, d: _set(t, name="NCT00000009"),
    "trial_type": lambda r, g, t, d: _set(t, entity_type="Drug"),
    "trial_external_ids": lambda r, g, t, d: _set(t, external_ids={"NCT": "NCT00000009"}),
    "trial_property_added": lambda r, g, t, d: t.properties.update(extra=1),
    "trial_property_dropped": lambda r, g, t, d: t.properties.clear(),
    "trial_field_dropped": lambda r, g, t, d: t.field_evidence.pop(),
    "trial_field_value": lambda r, g, t, d: _set(t.field_evidence[-1], value="changed"),
    "trial_field_pointer": lambda r, g, t, d: _set(t.field_evidence[0], json_pointer="/protocolSection/conditionsModule/conditions/0"),
    "trial_basis_pointer": lambda r, g, t, d: _set(t.identity_rule.basis, json_pointer="/protocolSection/conditionsModule/conditions/0",
                                                  value="Condition A"),
    "trial_rule_value": lambda r, g, t, d: _set(t.identity_rule, value="NCT00000009"),
    "trial_evidence_other_doc": lambda r, g, t, d: _set(t.field_evidence[0], raw_sha256="0" * 64),
    "trial_mention": lambda r, g, t, d: t.mention_ids.append("m"),
    "child_review_pending": lambda r, g, t, d: _set(_node_of(g, "Condition A").review, status="pending"),
    "child_alias": lambda r, g, t, d: _node_of(g, "Condition A").aliases.append("a"),
    "child_external_ids": lambda r, g, t, d: _set(_node_of(g, "Condition A"), external_ids={"X": "1"}),
    "child_properties": lambda r, g, t, d: _node_of(g, "Condition A").properties.update(x=1),
    "child_type": lambda r, g, t, d: _set(_node_of(g, "Condition A"), entity_type="Drug"),
    "child_name": lambda r, g, t, d: _set(_node_of(g, "Condition A"), name="Condition B"),
    "child_namespace": lambda r, g, t, d: _set(_node_of(g, "Condition A"), namespace="clinicaltrials.NCT00000009.condition"),
    "child_id": lambda r, g, t, d: _set(_node_of(g, "Condition A"), id=sha256("other")),
    "child_no_field_evidence": lambda r, g, t, d: _set(_node_of(g, "Condition A"), field_evidence=[]),
    "child_evidence_pointer": lambda r, g, t, d: _set(_node_of(g, "Condition A").field_evidence[0],
                                                     json_pointer="/protocolSection/identificationModule/nctId", value="NCT00000001"),
    "child_confirmed_without_rule": lambda r, g, t, d: _set(_node_of(g, "Condition A"), identity_status="confirmed"),
    "edge_type": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"), relation_type="REGISTERED_SPONSOR"),
    "edge_review_pending": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION").review, status="pending"),
    "edge_unbound": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"), binding_status="unbound"),
    "edge_method": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"), relation_props={"method_version": "x"}),
    "edge_method_v1_without_legacy": lambda r, g, t, d: [_set(e, relation_props={"method_version": LEGACY_METHOD}) for e in g.edges],
    "edge_method_mixed": lambda r, g, t, d: _set(g.edges[0], relation_props={"method_version": LEGACY_METHOD}),
    "edge_extra_props": lambda r, g, t, d: _edge_of(g, "REGISTERED_CONDITION").relation_props.update(x=1),
    "edge_valid_from": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"),
                                              valid_from=TimeValue(value="2016", precision="year", meaning="guessed_from_document")),
    "edge_valid_to": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"),
                                            valid_to=TimeValue(value="2016", precision="year", meaning="guessed_from_document")),
    "edge_no_field_evidence": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"), field_evidence=[]),
    "edge_evidence_value": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION").field_evidence[0], value="Condition B"),
    "edge_evidence_pointer": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION").field_evidence[0],
                                                    json_pointer="/protocolSection/conditionsModule/conditions/1"),
    "edge_evidence_not_registered_field": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION").field_evidence[0],
                                                                 json_pointer="/protocolSection/identificationModule/nctId",
                                                                 value="NCT00000001"),
    "edge_source_child": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"), source=_edge_of(g, "REGISTERED_CONDITION").target),
    "edge_target_other_child": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"),
                                                      target=_edge_of(g, "REGISTERED_SPONSOR").target),
    "edge_evidence_unlisted_on_target": lambda r, g, t, d: _node_of(g, "Condition A").field_evidence.clear(),
    "edge_evidence": lambda r, g, t, d: _set(_edge_of(g, "REGISTERED_CONDITION"), evidence=[{"x": 1}]),
}
# changes outside the rules the validator owns: structural checks of the model already ran, so they stay valid
ACCEPTED = set()


def _documents(rec):
    return context(rec)


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_forged_change_to_a_native_graph_is_rejected(name):
    rec, graph, trial, _children = _graph()
    documents = _documents(rec)
    try:
        MUTATIONS[name](rec, graph, trial, documents)
    except (TypeError, ValueError):  # the model refuses this change itself, before the validator is reached
        return
    if name in ACCEPTED:
        validate_field_bindings(graph, documents)
        return
    with pytest.raises(HistoryError) as error:
        validate_field_bindings(graph, documents)
    assert error.value.code == "graph_field_mismatch"


def test_valid_native_graph_is_accepted():
    rec, graph, _trial, _children = _graph()
    validate_field_bindings(graph, _documents(rec))
    validate_field_bindings(graph, _documents(rec), allow_legacy=True)


def _v1(graph, trial):
    for node in graph.nodes:
        node.properties = {}
    trial.field_evidence = []
    for edge in graph.edges:
        edge.relation_props = {"method_version": LEGACY_METHOD}


def test_v1_graph_is_valid_only_for_fixed_reads():
    rec, graph, trial, _children = _graph()
    _v1(graph, trial)
    validate_field_bindings(graph, _documents(rec), allow_legacy=True)
    with pytest.raises(HistoryError):
        validate_field_bindings(graph, _documents(rec))


def test_v1_graph_cannot_carry_v2_attributes_or_collaborator_edges():
    rec, graph, trial, _children = _graph()
    _v1(graph, trial)
    trial.properties = {"x": 1}
    with pytest.raises(HistoryError):
        validate_field_bindings(graph, _documents(rec), allow_legacy=True)
    rec, graph, trial, _children = _graph()
    _v1(graph, trial)
    trial.field_evidence = _graph()[2].field_evidence
    with pytest.raises(HistoryError):
        validate_field_bindings(graph, _documents(rec), allow_legacy=True)
