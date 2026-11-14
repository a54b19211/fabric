"""固定图查询/原生字段规则的纯计算反例；不连接任何服务。"""

import json
from datetime import datetime, timezone

import pytest

from graph.publish.graph_tools import graph_identity, paths, subgraph
from graph.publish.native_graph import build_native_graph, validate_field_bindings
from graph.publish.reviewed_graph import GraphIdentity, GraphRef, resolve
from store.record_models import AvailabilityProof, FabricError, VersionRecord, sha256


def record(nct="NCT00000001", *, condition="Condition A"):
    raw = json.dumps({"protocolSection": {
        "identificationModule": {"nctId": nct},
        "conditionsModule": {"conditions": [condition]},
        "armsInterventionsModule": {"interventions": [{"name": "Drug A"}]},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "SYNTHETIC University"}},
    }})
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    return VersionRecord(source_id="clinicaltrials", source_record_id=nct,
                         version_id=sha256("version:" + raw), record_kind="trial", is_demo=True,
                         title=nct, raw_content=raw, raw_sha256=sha256(raw), clean_text=nct,
                         facts={"trial_id": nct}, field_locators={}, first_seen_at=now,
                         received_at=now, availability_proof=AvailabilityProof(kind="unverified"), published_seq=1)


def frozen_ref():
    return GraphRef(source_id="reviewed_graph", source_record_id="synthetic-native",
                    version_id="a" * 64, raw_sha256="b" * 64, is_demo=True,
                    first_seen_at="2026-10-30T00:00:00Z")


def context(rec):
    return {(rec.source_id, rec.source_record_id, rec.version_id, rec.raw_sha256, None): rec.model_dump(mode="json")}


def test_native_trial_identity_and_source_relations_do_not_need_manual_approval():
    rec = record()
    graph = build_native_graph([rec])
    trial = next(node for node in graph.nodes if node.entity_type == "ClinicalTrial")
    assert trial.identity_status == "rule_confirmed" and trial.id == rec.source_record_id
    assert trial.review.status == "rule_confirmed"
    assert next(node for node in graph.nodes if node.name == "SYNTHETIC University").entity_type == "Organization"
    assert trial.identity_rule.basis.json_pointer.endswith("/nctId")
    assert {edge.relation_type for edge in graph.edges} == {
        "REGISTERED_CONDITION", "REGISTERED_INTERVENTION", "REGISTERED_SPONSOR",
    }
    assert all(edge.binding_status == "bound" and edge.field_evidence for edge in graph.edges)
    assert all(edge.valid_from is None and edge.valid_to is None for edge in graph.edges)
    assert all(node.identity_status == "unresolved" for node in graph.nodes if node != trial)
    assert resolve(graph, frozen_ref(), external_namespace="NCT", external_id=trial.id).candidates == [trial]
    validate_field_bindings(graph, context(rec))


@pytest.mark.parametrize("change", ["value", "pointer", "predicate", "identity", "alias", "time"])
def test_source_binding_rejects_forged_fields_meaning_and_identity(change):
    rec = record()
    graph = build_native_graph([rec])
    edge = graph.edges[0]
    trial = next(node for node in graph.nodes if node.entity_type == "ClinicalTrial")
    if change == "value":
        edge.field_evidence[0].value = "Different condition"
    elif change == "pointer":
        edge.field_evidence[0].json_pointer = "/protocolSection/missing"
    elif change == "predicate":
        edge.relation_type = "CAUSES"
    elif change == "identity":
        trial.identity_rule.basis.value = "NCT99999999"
    elif change == "alias":
        trial.aliases.append("Unproven synonym")
    else:
        from store.record_models import TimeValue
        edge.valid_from = TimeValue(value="2016", precision="year", meaning="guessed_from_document")
    with pytest.raises(FabricError) as error:
        validate_field_bindings(graph, context(rec))
    assert error.value.code == "graph_field_mismatch"


def test_same_trial_two_versions_cannot_silently_mix_and_budget_is_visible():
    old, new = record(), record(condition="Condition B")
    with pytest.raises(FabricError):
        build_native_graph([old, new])
    bounded = build_native_graph([old], max_nodes=2, max_edges=1)
    assert len(bounded.nodes) == 2 and len(bounded.edges) == 1
    assert bounded.selection_truncated
    other = record("NCT00000002")
    assert build_native_graph([old, other]).model_dump() == build_native_graph([other, old]).model_dump()


def test_path_direction_scope_and_original_fields_are_preserved():
    graph = build_native_graph([record()])
    trial = graph_identity("clinicaltrials.NCT|NCT00000001")
    child = graph.edges[0].target
    assert paths(graph, frozen_ref(), trial, child).status == "not_found"  # named child is unresolved
    result = paths(graph, frozen_ref(), trial, child, review_scope="with_candidates")
    assert result.status == "found" and result.paths[0].edge_ids == [graph.edges[0].id]
    assert result.edges[0].field_evidence == graph.edges[0].field_evidence
    assert result.graph_ref.version_id == frozen_ref().version_id
    assert paths(graph, frozen_ref(), child, trial).status == "not_found"
    assert paths(graph, frozen_ref(), child, trial, direction="in", review_scope="with_candidates").status == "found"
    graph.edges[0].review.status = "pending"
    assert paths(graph, frozen_ref(), trial, child).status == "not_found"
    candidate = paths(graph, frozen_ref(), trial, child, review_scope="with_candidates")
    assert candidate.status == "found" and candidate.edges[0].review.status == "pending"
    graph.edges[0].binding_status = "unbound"
    graph.edges[0].review.status = "approved"
    assert paths(graph, frozen_ref(), trial, child).status == "not_found"


def test_paths_have_hard_search_budget_and_do_not_silently_complete():
    graph = build_native_graph([record()])
    result = paths(graph, frozen_ref(), graph.edges[0].target, graph.edges[1].target,
                   direction="both", max_depth=1, review_scope="with_candidates")
    assert not result.paths and result.coverage.truncated
    assert result.coverage.stop_reason == "depth_budget"
    bounded = paths(graph, frozen_ref(), graph.edges[0].target, graph.edges[1].target,
                    direction="both", max_expansions=1, review_scope="with_candidates")
    assert bounded.explored_states == 1 and bounded.coverage.stop_reason == "search_budget"
    assert bounded.coverage.truncated


def test_induced_subgraph_returns_only_requested_endpoints_and_exposes_caps():
    graph = build_native_graph([record()])
    trial = GraphIdentity(namespace="clinicaltrials.NCT", id="NCT00000001")
    selected = subgraph(graph, frozen_ref(), [trial, graph.edges[0].target], review_scope="with_candidates")
    assert len(selected.nodes) == 2 and len(selected.edges) == 1
    assert selected.edges[0].source == trial
    assert len(subgraph(graph, frozen_ref(), [trial]).edges) == 0
    limited = subgraph(graph, frozen_ref(), [trial, graph.edges[0].target], max_nodes=1, review_scope="with_candidates")
    assert limited.coverage.truncated and not limited.edges
    with pytest.raises(FabricError):
        subgraph(graph, frozen_ref(), [trial, trial])


@pytest.mark.parametrize("value", ["NCBI1956", "x|y|z", "x| "])
def test_identity_is_never_guessed_from_malformed_input(value):
    with pytest.raises(FabricError):
        graph_identity(value)


@pytest.mark.parametrize("injection", ["empty_projection", "approved_node", "approved_edge", "redirect"])
def test_native_origin_cannot_smuggle_approved_objects_or_edges_without_native_rules(injection):
    from graph.publish.reviewed_graph import GraphBundle, GraphEdge, GraphNode, GraphRedirect, GraphReview

    rec = record()
    graph = build_native_graph([rec])
    unsupported = GraphNode(namespace="synthetic", id="unsupported", entity_type="Drug",
                            name="SYNTHETIC unsupported", identity_status="confirmed",
                            identity_review_basis="unverified assertion",
                            review=GraphReview(status="approved"))
    if injection == "empty_projection":
        graph = GraphBundle(graph_kind="reviewed", origin="source_field_projection",
                            namespace="clinicaltrials.source_fields", is_demo=True,
                            title="SYNTHETIC invalid", scope_description="SYNTHETIC invalid",
                            nodes=[unsupported], edges=[])
    elif injection == "approved_node":
        graph.nodes.append(unsupported)
    elif injection == "approved_edge":
        graph.edges.append(GraphEdge(id="synthetic:unsupported", source=graph.edges[0].source,
                                    target=graph.edges[0].target, relation_type="CAUSES",
                                    review=GraphReview(status="approved"), binding_status="unbound"))
    else:
        graph.redirects.append(GraphRedirect(source=GraphIdentity(namespace="synthetic", id="alias"),
                                             target=graph.edges[0].source, merge_log_id="synthetic"))
    with pytest.raises(FabricError) as error:
        validate_field_bindings(graph, context(rec))
    assert error.value.code == "graph_field_mismatch"
