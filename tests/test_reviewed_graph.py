"""图发布契约/有限遍历的合成反例；不调用现役图、模型或外部网络。"""

import json

import httpx
import pytest
from pydantic import ValidationError

from biz_rules.history_models import HistoryError, canonical, sha256
from biz_rules.reviewed_graph import (
    GraphBundle,
    GraphEdge,
    GraphIdentity,
    GraphNode,
    GraphRef,
    GraphReview,
    StoredDocument,
    document_native_id,
    neighborhood,
    primekg_bundle,
    resolve,
)


def node(identity, name="same name", **extra):
    return GraphNode(
        **{
            "namespace": "fixture.entity_dict",
            "id": identity,
            "entity_type": "Drug",
            "name": name,
            "identity_status": "unresolved",
            "review": GraphReview(status="auto_approved"),
            **extra,
        }
    )


def bundle(nodes=None, edges=None, **extra):
    return GraphBundle(
        graph_kind="reviewed",
        namespace="fixture",
        is_demo=True,
        title="明确合成图",
        origin="explicit_demo",
        scope_description="synthetic selected graph",
        nodes=nodes or [node("1")],
        edges=edges or [],
        **extra,
    )


def ref():
    return GraphRef(
        source_id="reviewed_graph",
        source_record_id="fixture-graph",
        version_id="a" * 64,
        raw_sha256="b" * 64,
        is_demo=True,
        first_seen_at="2026-10-18T00:00:00Z",
    )


def test_exact_resolution_keeps_ambiguity_and_namespaces():
    a = node("1", aliases=["approved local alias"], external_ids={"DrugBank": "DB001"})
    b = node("2")
    graph = bundle([a, b])
    result = resolve(graph, ref(), name="SAME NAME", limit=1)
    assert (
        result.status == "ambiguous"
        and result.truncated
        and len(result.candidates) == 1
    )
    assert resolve(graph, ref(), name="approved local alias").candidates == [a]
    assert resolve(
        graph, ref(), external_namespace="DrugBank", external_id="DB001"
    ).candidates == [a]
    assert resolve(graph, ref(), name="not a synonym").status == "not_found"
    with pytest.raises(ValidationError):
        GraphIdentity(namespace="a|b", id="c")


def test_direction_and_node_budget_preserve_stored_endpoints():
    a, b, c = node("1"), node("2"), node("3")
    edges = [
        GraphEdge(
            id="ab",
            source=GraphIdentity(namespace=a.namespace, id=a.id),
            target=GraphIdentity(namespace=b.namespace, id=b.id),
            relation_type="TARGETS",
            review=GraphReview(status="approved"),
            binding_status="unbound",
        ),
        GraphEdge(
            id="cb",
            source=GraphIdentity(namespace=c.namespace, id=c.id),
            target=GraphIdentity(namespace=b.namespace, id=b.id),
            relation_type="TARGETS",
            review=GraphReview(status="approved"),
            binding_status="unbound",
        ),
    ]
    graph = bundle([a, b, c], edges)
    assert neighborhood(graph, ref(), [b], direction="out").edges == []
    result = neighborhood(graph, ref(), [b], direction="in", max_nodes=2, max_depth=2)
    assert result.coverage.truncated and len(result.nodes) == 2
    assert len(result.edges) == 1 and result.edges[0].source == edges[0].source
    assert result.edges[0].target == edges[0].target
    assert all(
        edge.source.id in {n.id for n in result.nodes}
        and edge.target.id in {n.id for n in result.nodes}
        for edge in result.edges
    )


def test_unverified_identity_and_bound_without_quote_are_rejected():
    with pytest.raises(ValidationError):
        node("1", identity_status="confirmed")
    with pytest.raises(ValidationError):
        GraphEdge(
            id="bad",
            source=GraphIdentity(namespace="fixture", id="1"),
            target=GraphIdentity(namespace="fixture", id="2"),
            relation_type="TARGETS",
            review=GraphReview(status="approved"),
            binding_status="bound",
        )
    with pytest.raises(ValidationError):
        bundle(
            edges=[
                GraphEdge(
                    id="dangling",
                    source=GraphIdentity(namespace="fixture", id="1"),
                    target=GraphIdentity(namespace="fixture", id="2"),
                    relation_type="TARGETS",
                    review=GraphReview(status="approved"),
                    binding_status="unbound",
                )
            ]
        )


def test_primekg_current_capture_is_background_and_has_more_is_not_complete():
    # 这是HTTP捕获形状的合成输入；没有外部实际调用。
    capture = {
        "is_demo": False,
        "source": "primekg_current_projection",
        "observed_at": "2026-10-18T00:00:00Z",
        "seed": {"node_source": "NCBI", "primekg_id": "1956"},
        "has_more": True,
        "rows": [
            {
                "from_node": {
                    "name": "EGFR",
                    "source": "NCBI",
                    "id": "1956",
                    "type": "gene/protein",
                },
                "to_node": {
                    "name": "lung disease",
                    "source": "MONDO",
                    "id": "0000001",
                    "type": "disease",
                },
                "relation_type": "disease_protein",
                "properties": {"display_relation": "associated with"},
            }
        ],
    }
    graph = primekg_bundle(capture)
    assert graph.selection_truncated and graph.graph_kind == "background"
    assert graph.edges[0].relation_type == "disease_protein"
    assert graph.edges[0].source.namespace == "primekg.NCBI"
    assert graph.edges[0].binding_status == "background" and not graph.document_refs
    assert all(n.identity_status == "background" for n in graph.nodes)


def test_legacy_document_preserves_raw_and_rejects_hash_mismatch():
    raw = "原文药物甲与蛋白乙。"
    document = StoredDocument(
        source_doc_uuid="uuid",
        source_site="clinicaltrials",
        title="old raw",
        original_raw_content=raw,
        original_raw_sha256=sha256(raw),
    )
    assert document.original_raw_content == raw
    assert document_native_id(document) == "legacy:clinicaltrials:uuid"
    with pytest.raises(ValidationError):
        StoredDocument.model_validate(
            {**document.model_dump(), "original_raw_content": raw + "changed"}
        )


def test_auto_mention_cannot_become_a_confirmed_alias_of_shared_dict_id():
    from biz_rules.reviewed_graph import GraphDocumentRef
    from flows.graph_publish import build_document_bundle

    raw = "manual and auto alias"
    document = StoredDocument(
        source_doc_uuid="selected",
        source_site="external_archive",
        title="明确合成",
        original_raw_content=raw,
        original_raw_sha256=sha256(raw),
    )
    reference = GraphDocumentRef(
        source_id="legacy_documents",
        source_record_id=document_native_id(document),
        version_id="a" * 64,
        raw_sha256="b" * 64,
        origin_doc_uuid="selected",
    )
    observation = {
        "schema_version": "datafabric.legacy.graph.observation.v1",
        "namespace": "fixture",
        "is_demo": True,
        "selected_document_uuids": ["selected"],
        "scope_record_ids": ["selected"],
        "verified_dict_ids": ["10"],
        "relations": [],
        "entities": [
            {
                "id": 1,
                "source_doc_uuid": "selected",
                "review_status": "approved",
                "dict_id": 10,
                "entity_type": "Drug",
                "entity_name": "manual",
                "source_text": "manual",
            },
            {
                "id": 2,
                "source_doc_uuid": "selected",
                "review_status": "auto_approved",
                "dict_id": 10,
                "entity_type": "Drug",
                "entity_name": "auto alias",
                "source_text": "auto alias",
            },
        ],
    }
    # Legacy is_verified alone must stay a proposal.
    old = build_document_bundle(observation, [document], [reference])
    assert all(node.identity_status == "unresolved" for node in old.nodes)
    reviewed = "2026-10-31T00:00:00Z"
    observation["dictionary_context"] = {"10": {
        "id": 10, "is_verified": True, "verified_by": "synthetic-reviewer",
        "verified_at": reviewed, "verification_basis": "synthetic identity check",
    }}
    observation["entities"][0].update(
        identity_confirmed_by="synthetic-reviewer", identity_confirmed_at=reviewed,
        identity_confirmation_basis="synthetic fixed evidence check",
        current_review_evidence=[{**reference.model_dump(), "source_doc_uuid": "selected", "quote": "manual"}],
    )
    graph = build_document_bundle(observation, [document], [reference])
    assert len(graph.nodes) == 2
    canonical = next(
        node for node in graph.nodes if node.namespace.endswith(".entity_dict")
    )
    proposed = next(
        node for node in graph.nodes if node.namespace.endswith(".extracted_entity")
    )
    assert canonical.identity_status == "confirmed" and canonical.aliases == []
    assert [ref.quote for ref in canonical.evidence] == ["manual"]
    assert (
        proposed.identity_status == "unresolved"
        and proposed.review.status == "auto_approved"
    )
    resolved = resolve(graph, ref(), name="auto alias")
    assert resolved.candidates == [proposed]
    # Reusing the same quote from another revision does not carry confirmation across versions.
    observation["entities"][0]["evidence"] = observation["entities"][0]["current_review_evidence"]
    observation["entities"][0]["current_review_evidence"] = [{**observation["entities"][0]["evidence"][0], "version_id": "c" * 64}]
    stale = build_document_bundle(observation, [document], [reference])
    assert all(node.identity_status == "unresolved" for node in stale.nodes)
    observation["entities"].append(observation["entities"][1])
    with pytest.raises(HistoryError) as error:
        build_document_bundle(observation, [document], [reference])
    assert error.value.code == "duplicate_legacy_mention"


def test_derived_fact_locators_follow_the_existing_history_json_pointer_contract():
    from biz_rules.history_models import ArchiveRecord
    from biz_rules.history_sources import _pointer, parse_json, project_record

    graph = bundle()
    raw = graph.model_dump_json()
    projected = project_record(
        "reviewed_graph",
        ArchiveRecord(source_record_id="graph", record_kind="graph", raw_content=raw),
    )
    source = parse_json(raw)
    assert all(
        _pointer(source, locator["json_pointer"]) == projected["facts"][key]
        for key, locator in projected["field_locators"].items()
    )
    assert projected["parser_version"] == "datafabric.graph.projection.v0.2"
    document = StoredDocument(
        source_doc_uuid="doc",
        source_site="clinicaltrials",
        title="legacy",
        original_raw_content="保留原文",
        original_raw_sha256=sha256("保留原文"),
    )
    raw = document.model_dump_json()
    projected = project_record(
        "legacy_documents",
        ArchiveRecord(
            source_record_id=document_native_id(document),
            record_kind="document",
            raw_content=raw,
        ),
    )
    source = parse_json(raw)
    assert all(
        _pointer(source, locator["json_pointer"]) == projected["facts"][key]
        for key, locator in projected["field_locators"].items()
    )
    assert projected["clean_text"] == "保留原文"


def test_missing_optional_graph_fact_has_no_invented_null_locator():
    from biz_rules.history_models import ArchiveRecord
    from biz_rules.history_sources import project_record

    raw = bundle().model_dump_json(exclude={"selection_truncated"})
    projected = project_record(
        "reviewed_graph",
        ArchiveRecord(source_record_id="graph", record_kind="graph", raw_content=raw),
    )
    assert "selection_truncated" not in projected["facts"]
    assert "selection_truncated" not in projected["field_locators"]


def test_primekg_capture_is_parameterized_read_only_bounded_and_directional(
    monkeypatch,
):
    from contextlib import contextmanager

    import flows.graph_publish as publish
    from flows.graph_publish import capture_primekg

    @contextmanager
    def connection():
        yield object()

    class Engine:
        begin = staticmethod(connection)

    monkeypatch.setattr(publish.store, "history_engine", lambda: Engine())
    monkeypatch.setattr(publish.store, "_source", lambda conn, source: None)
    for key, value in {
        "URL": "http://127.0.0.1:7474",
        "USER": "test-local-reader",
        "PASSWORD": "not-real-password",
    }.items():
        monkeypatch.setenv("DATAFABRIC_GRAPH_NEO4J_" + key, value)
    seed = {
        "name": "EGFR",
        "node_source": "NCBI",
        "primekg_id": "1956",
        "primekg_type": "gene/protein",
    }
    neighbor = {
        "name": "lung disease",
        "node_source": "MONDO",
        "primekg_id": "1",
        "primekg_type": "disease",
    }
    requests = []

    def respond(request):
        body = json.loads(request.content)
        statement = body["statements"][0]
        assert request.headers["Access-Mode"] == "READ"
        assert (
            "CREATE " not in statement["statement"]
            and "MERGE " not in statement["statement"]
        )
        assert statement["parameters"]["neighbor_term"] == "lung' literal"
        assert "lung' literal" not in statement["statement"]
        assert statement["parameters"]["limit_plus_one"] == 2
        requests.append(request.url.path)
        row = [
            1,
            seed,
            neighbor,
            seed,
            "disease_protein",
            {"display_relation": "associated with"},
        ]
        return httpx.Response(
            200,
            json={
                "errors": [],
                "results": [
                    {
                        "columns": [
                            "seed_count",
                            "seed",
                            "from_node",
                            "to_node",
                            "relation_type",
                            "properties",
                        ],
                        "data": [{"row": row}, {"row": row}],
                    }
                ],
            },
        )

    client = httpx.Client(
        base_url="http://127.0.0.1:7474", transport=httpx.MockTransport(respond)
    )
    observation = capture_primekg(
        node_type="gene/protein",
        node_source="NCBI",
        seed_id="1956",
        neighbor_term="lung' literal",
        limit=1,
        client=client,
    )
    assert requests == ["/db/neo4j/tx/commit"] and observation["has_more"]
    assert observation["rows"][0]["from_node"]["source"] == "MONDO"
    assert observation["rows"][0]["to_node"]["id"] == "1956"
    assert primekg_bundle(observation).selection_truncated
    client.close()


def test_primekg_capture_separates_empty_error_and_ambiguous_identity(monkeypatch):
    from contextlib import contextmanager

    import flows.graph_publish as publish
    from flows.graph_publish import capture_primekg

    @contextmanager
    def connection():
        yield object()

    class Engine:
        begin = staticmethod(connection)

    monkeypatch.setattr(publish.store, "history_engine", lambda: Engine())
    monkeypatch.setattr(publish.store, "_source", lambda conn, source: None)
    for key, value in {
        "URL": "http://127.0.0.1:7474",
        "USER": "test-reader",
        "PASSWORD": "not-real-password",
    }.items():
        monkeypatch.setenv("DATAFABRIC_GRAPH_NEO4J_" + key, value)
    seed = {
        "name": "EGFR",
        "node_source": "NCBI",
        "primekg_id": "1956",
        "primekg_type": "gene/protein",
    }
    columns = [
        "seed_count",
        "seed",
        "from_node",
        "to_node",
        "relation_type",
        "properties",
    ]
    responses = [
        {
            "errors": [],
            "results": [
                {
                    "columns": columns,
                    "data": [{"row": [1, seed, None, None, None, None]}],
                }
            ],
        },
        {"errors": [{"message": "do not echo source diagnostics"}], "results": []},
        {
            "errors": [],
            "results": [
                {
                    "columns": columns,
                    "data": [{"row": [2, None, None, None, None, None]}],
                }
            ],
        },
    ]
    client = httpx.Client(
        base_url="http://127.0.0.1:7474",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=responses.pop(0))
        ),
    )
    observation = capture_primekg(
        node_type="gene/protein", node_source="NCBI", seed_id="1956", client=client
    )
    assert (
        observation["seed_found"]
        and not observation["rows"]
        and not observation["has_more"]
    )
    assert len(primekg_bundle(observation).nodes) == 1
    with pytest.raises(HistoryError) as error:
        capture_primekg(
            node_type="gene/protein", node_source="NCBI", seed_id="1956", client=client
        )
    assert (
        error.value.code == "primekg_query_failed"
        and "do not echo" not in error.value.message
    )
    with pytest.raises(HistoryError) as error:
        capture_primekg(
            node_type="gene/protein", node_source="NCBI", seed_id="1956", client=client
        )
    assert error.value.code == "ambiguous_primekg_seed"
    client.close()

def test_two_source_documents_can_share_mentions_without_borrowing_confirmation():
    from biz_rules.reviewed_graph import GraphDocumentRef
    from flows.graph_publish import build_document_bundle
    text = "SYNTHETIC A targets SYNTHETIC B."
    documents = [StoredDocument(source_doc_uuid=uuid, source_site="external_archive",
        title="Synthetic", original_raw_content=text, original_raw_sha256=sha256(text))
        for uuid in ("one", "two")]
    refs = [GraphDocumentRef(source_id="legacy_documents", source_record_id=document_native_id(doc),
        version_id=version * 64, raw_sha256=raw * 64, origin_doc_uuid=doc.source_doc_uuid)
        for doc, version, raw in zip(documents, ("a", "c"), ("b", "d"))]
    entities, relations = [], []
    for doc, source_ref in zip(documents, refs):
        for ident, name in [(1, "SYNTHETIC A"), (2, "SYNTHETIC B")]:
            entities.append({"id": ident, "source_doc_uuid": "one", "observation_source_doc_uuid": doc.source_doc_uuid,
                "entity_type": "Drug", "entity_name": name, "review_status": "approved",
                "dict_id": ident, "source_text": name, "evidence": [{
                    **source_ref.model_dump(), "source_doc_uuid": doc.source_doc_uuid, "quote": name}]})
        relations.append({"id": 3, "source_doc_uuid": "one", "observation_source_doc_uuid": doc.source_doc_uuid,
            "from_entity_id": 1, "to_entity_id": 2, "relation_type": "TARGETS",
            "source_text": text, "review_status": "approved"})
    observation = {"schema_version": "datafabric.legacy.graph.observation.v1", "namespace": "fixture",
        "is_demo": True, "selected_document_uuids": ["one", "two"], "scope_record_ids": ["one", "two"],
        "verified_dict_ids": ["1", "2"], "entities": entities, "relations": relations}
    graph = build_document_bundle(observation, documents, refs)
    assert len(graph.nodes) == 2 and len(graph.edges) == 2
    assert len({edge.id for edge in graph.edges}) == 2
    assert all(node.identity_status == "unresolved" for node in graph.nodes)
    assert all({proof.origin_doc_uuid for proof in node.evidence} == {"one", "two"} for node in graph.nodes)
