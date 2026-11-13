"""受限图查询的边界测试：合成小图和本地HTTP替身，不访问现役库。"""

import json
from contextlib import contextmanager

import httpx
import pytest
from pydantic import ValidationError

from graph.publish.reviewed_graph import GraphEdge, GraphReview, PrimeKGQuery, neighborhood
from store.history_models import HistoryError
from tests.test_reviewed_graph import bundle, node, ref


def test_depth_limit_reports_only_unvisited_eligible_edges():
    a, b, c = node("1"), node("2"), node("3")
    def edge(identity, source, target):
        return GraphEdge(id=identity, source=source, target=target,
                         relation_type="TARGETS", review=GraphReview(status="approved"), binding_status="unbound")
    graph = bundle([a, b, c], [edge("ab", a, b), edge("bc", b, c)])
    result = neighborhood(graph, ref(), [a], max_depth=1)
    assert result.coverage.truncated
    assert result.coverage.stop_reason == "depth_budget"
    assert [e.id for e in result.edges] == ["ab"]
    assert not neighborhood(graph, ref(), [a], max_depth=2).coverage.truncated
    assert not neighborhood(graph, ref(), [a, b], max_depth=1).coverage.truncated
    assert not neighborhood(graph, ref(), [a], direction="in").coverage.truncated
    assert not neighborhood(graph, ref(), [a], relation_types=["OTHER"]).coverage.truncated
    assert neighborhood(graph, ref(), [a], max_depth=0).coverage.stop_reason == "depth_budget"


@pytest.mark.parametrize("extra", [
    {"node_source": "a" * 153}, {"seed_id": "a|b"}, {"limit": True},
    {"limit": 201}, {"cypher": "MATCH (n) RETURN n"}, {"cutoff": "2015-01-01"},
])
def test_query_rejects_invalid_scope_before_access(extra):
    with pytest.raises(ValidationError):
        PrimeKGQuery(**({"node_type": "gene/protein", "node_source": "NCBI", "seed_id": "1956"} | extra))


def test_capture_scope_and_timeout_are_explicit(monkeypatch):
    import graph.publish.graph_publish as publish
    @contextmanager
    def connection():
        yield object()
    class Engine:
        begin = staticmethod(connection)
    monkeypatch.setattr(publish.store, "history_engine", lambda: Engine())
    monkeypatch.setattr(publish.store, "_source", lambda *args: None)
    for key, value in {"URL": "http://127.0.0.1:7474", "USER": "fixture", "PASSWORD": "fixture"}.items():
        monkeypatch.setenv("DATAFABRIC_GRAPH_NEO4J_" + key, value)
    query = PrimeKGQuery(node_type="gene/protein", node_source="NCBI", seed_id="1956",
                        relation_types=["b", "a", "b"], neighbor_term="lung", limit=3)
    def respond(request):
        assert json.loads(request.content)["statements"][0]["parameters"]["relation_types"] == ["a", "b"]
        return httpx.Response(200, json={"errors": [], "results": [{
            "columns": ["seed_count", "seed", "from_node", "to_node", "relation_type", "properties"],
            "data": [{"row": [0, None, None, None, None, None]}],
        }]})
    with httpx.Client(base_url="http://127.0.0.1:7474", transport=httpx.MockTransport(respond)) as client:
        result = publish.capture_primekg(**query.model_dump(), client=client)
    assert result["query_parameters"] == query.model_dump()
    assert result["query"] == query.scope() and result["seed_found"] is False
    def timeout(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)
    with httpx.Client(base_url="http://127.0.0.1:7474", transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(HistoryError) as error:
            publish.capture_primekg(**query.model_dump(), client=client)
    assert error.value.code == "primekg_timeout" and error.value.status == 504


@pytest.mark.parametrize("bad_count", [{}, []])
def test_malformed_capture_count_stays_a_protocol_error(monkeypatch, bad_count):
    import graph.publish.graph_publish as publish

    monkeypatch.setattr(
        publish, "_read_primekg",
        lambda *args, **kwargs: [[bad_count, None, None, None, None, None]],
    )
    with pytest.raises(HistoryError) as error:
        publish.capture_primekg(node_type="gene/protein", node_source="NCBI", seed_id="1956")
    assert error.value.code == "primekg_invalid_response" and error.value.status == 503
