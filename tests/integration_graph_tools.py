"""真实隔离History库：登记字段绑定、固定版本与有界图查询。"""

from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import text

from integration_history import pg, pg_engine  # noqa: F401
from biz_rules.history_models import ArchiveImport, ArchiveRecord, HistoryError
from biz_rules.reviewed_graph import GraphBundle, GraphRef
from db import history as store
from flows.graph_publish import publish_native_graph


def source_record(condition="SYNTHETIC condition A"):
    return ArchiveRecord(source_record_id="NCT00000001", record_kind="trial", raw_content=json.dumps({
        "protocolSection": {
            "identificationModule": {"nctId": "NCT00000001", "briefTitle": "SYNTHETIC trial"},
            "conditionsModule": {"conditions": [condition]},
            "armsInterventionsModule": {"interventions": [{"name": "SYNTHETIC drug"}]},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "SYNTHETIC sponsor"}},
        },
    }))


def adopt(item, key):
    run = store.import_archive(ArchiveImport(source_id="clinicaltrials", is_demo=True,
        scope_description="explicit synthetic native fields", declared_count=1, declared_complete=False,
        records=[item]), key)
    return store.read_version(run.version_ids[0], "clinicaltrials", item.source_record_id,
                              True, "current", datetime.now(timezone.utc))


def prepared(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", json.dumps([
        "clinicaltrials", "reviewed_graph", "legacy_documents", "external_archive",
    ]))
    record = adopt(source_record(), "synthetic-native-source-a")
    run = publish_native_graph([record], "synthetic-native-graph-a")
    with pg.begin() as conn:
        payload = conn.execute(text("SELECT payload FROM history_versions WHERE version_id=:id"),
                               {"id": run.version_ids[0]}).scalar_one()
    return record, run, payload


def test_native_graph_real_publication_replay_and_old_fixed_field_version(pg, monkeypatch):
    source, run, old = prepared(pg, monkeypatch)
    assert run.state == "completed" and len(run.version_ids) == 1
    assert publish_native_graph([source], "synthetic-native-graph-a").version_ids == run.version_ids
    graph = GraphBundle.model_validate_json(old["raw_content"])
    assert len(graph.nodes) == 4 and len(graph.edges) == 3
    assert not any(node.entity_type == "Program" for node in graph.nodes)
    assert graph.nodes[0].identity_status == "rule_confirmed"
    assert graph.document_refs[0].version_id == source.version_id
    ref = GraphRef(**{key: old[key] for key in (
        "source_id", "source_record_id", "version_id", "raw_sha256", "is_demo", "first_seen_at",
    )})
    fixed_args = (ref.version_id, ref.source_id, ref.source_record_id,
                  ref.is_demo, "current", datetime.now(timezone.utc))
    seed_id = "clinicaltrials.NCT|NCT00000001"
    target_ids = [edge.target.namespace + "|" + edge.target.id for edge in graph.edges]
    assert store.read_graph_paths(*fixed_args, start=seed_id, target=target_ids[0]).status == "not_found"
    assert store.read_graph_paths(*fixed_args, start=seed_id, target=target_ids[0], review_scope="with_candidates").status == "found"
    linked = store.read_graph_paths(*fixed_args, start=target_ids[0], target=target_ids[2], direction="both", review_scope="with_candidates")
    assert len(linked.paths[0].edge_ids) == 2
    assert all(edge.field_evidence[0].version_id == source.version_id for edge in linked.edges)
    assert len(store.read_graph_subgraph(*fixed_args, node_ids=[seed_id, target_ids[0]], review_scope="with_candidates").edges) == 1

    revised = adopt(source_record("SYNTHETIC condition B"), "synthetic-native-source-b")
    new = publish_native_graph([revised], "synthetic-native-graph-b")
    assert new.version_ids != run.version_ids
    fixed = store.read_version(run.version_ids[0], "reviewed_graph", old["source_record_id"],
                               True, "current", datetime.now(timezone.utc))
    assert fixed.raw_sha256 == old["raw_sha256"] and "SYNTHETIC condition A" in fixed.raw_content
    assert "SYNTHETIC condition B" not in fixed.raw_content
    with pytest.raises(HistoryError) as error:
        store.read_version(new.version_ids[0], "reviewed_graph", old["source_record_id"],
                           True, "operational_replay", source.first_seen_at)
    assert error.value.code == "version_not_found"


def test_invalid_native_field_or_predicate_never_changes_real_published_graph(pg, monkeypatch):
    _source, _run, payload = prepared(pg, monkeypatch)
    for change in ("value", "predicate", "native_id", "approved_node", "approved_edge", "empty_projection"):
        graph = GraphBundle.model_validate_json(payload["raw_content"])
        if change == "value":
            graph.edges[0].field_evidence[0].value = "SYNTHETIC forged condition"
        elif change == "predicate":
            graph.edges[0].relation_type = "CAUSES"
        elif change == "native_id":
            graph.nodes[0].identity_rule.basis.json_pointer = "/protocolSection/conditionsModule/conditions/0"
        elif change == "approved_edge":
            from biz_rules.reviewed_graph import GraphEdge, GraphReview
            graph.edges.append(GraphEdge(id="synthetic:unsupported", source=graph.edges[0].source,
                                        target=graph.edges[0].target, relation_type="CAUSES",
                                        review=GraphReview(status="approved"), binding_status="unbound"))
        else:
            from biz_rules.reviewed_graph import GraphNode, GraphReview
            unsupported = GraphNode(namespace="synthetic", id="unsupported", entity_type="Drug",
                                    name="SYNTHETIC unsupported", identity_status="confirmed",
                                    identity_review_basis="unverified assertion", review=GraphReview(status="approved"))
            if change == "approved_node":
                graph.nodes.append(unsupported)
            else:
                graph = GraphBundle(graph_kind="reviewed", origin="source_field_projection",
                                    namespace="clinicaltrials.source_fields", is_demo=True,
                                    title="SYNTHETIC invalid", scope_description="SYNTHETIC invalid",
                                    nodes=[unsupported], edges=[])
        with pytest.raises(HistoryError) as error:
            store.import_archive(ArchiveImport(source_id="reviewed_graph", is_demo=True,
                scope_description="invalid synthetic field binding", declared_count=1,
                declared_complete=False, records=[ArchiveRecord(
                    source_record_id=payload["source_record_id"], record_kind="graph", raw_content=graph.model_dump_json(),
                )]), "synthetic-invalid-" + change)
        assert error.value.code == "graph_field_mismatch"
        with pg.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM history_versions WHERE source_id='reviewed_graph'")).scalar_one() == 1
