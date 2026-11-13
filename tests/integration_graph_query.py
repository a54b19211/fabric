"""PrimeKG查询的真实PG回归；所有图内容均为显式合成HTTP fixture。

仅替换Neo4j HTTP上游，不连接真实Neo4j；请求校验、捕获、采用及固定版本读取
保持真实。is_demo=False是当前查询API的固定契约，不表示这些合成图来自真实源。
复用既有夹具，仅允许127.0.0.1:18436/fabric_history并清理独立临时schema。
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Lock
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from integration_history import pg, pg_engine  # noqa: F401 -- shared PG fixtures
from sqlalchemy import text

from api.routers.history import router
from graph.api.history_graph import router as history_graph_router
from graph.publish import graph_publish, graph_query
from graph.publish.reviewed_graph import PrimeKGQuery
from store import history as store
from store.history_models import sha256

QUERY_TOKEN = "synthetic-primekg-query-only"
READ_TOKEN = "test-reader-only"
ADMIN_TOKEN = "test-admin-only"
CAPTURE_PATH = "/api/history/graph/primekg/capture"
LOOKUP_PATH = "/api/history/graph/primekg/lookup"
LOOKUP_QUERY = {"name": "synthetic native seed", "node_type": "gene/protein", "limit": 10}
NATIVE_CANDIDATE = {
    "name": "SYNTHETIC NATIVE SEED", "node_source": "SYNTHETIC_NATIVE_NCBI",
    "primekg_id": "00001956", "primekg_type": "gene/protein",
}
QUERY = {
    "node_type": "gene/protein",
    "node_source": "SYNTHETIC_NCBI",
    "seed_id": "SYNTHETIC_SEED",
    "relation_types": ["disease_protein", "drug_protein"],
    "neighbor_term": "SYNTHETIC",
    "limit": 5,
}
COLUMNS = [
    "seed_count", "seed", "from_node", "to_node", "relation_type", "properties",
]


@pytest.fixture
def graph_api(pg, monkeypatch):
    monkeypatch.setenv(
        "DATAFABRIC_HISTORY_SOURCE_ALLOWLIST",
        json.dumps(sorted(store.allowed_sources() | {"primekg_graph"})),
    )
    monkeypatch.setenv("DATAFABRIC_GRAPH_QUERY_TOKEN", QUERY_TOKEN)
    for suffix, value in {
        "URL": "http://127.0.0.1:17474",
        "USER": "synthetic-read-only",
        "PASSWORD": "synthetic-not-a-real-password",
        "DATABASE": "neo4j",
    }.items():
        monkeypatch.setenv("DATAFABRIC_GRAPH_NEO4J_" + suffix, value)

    state = {
        "mode": "edges", "neighbor_id": "SYNTHETIC_A", "requests": [],
        "barrier": None, "vary_per_request": False,
        "statements": [], "lookup_nodes": None, "force_empty_rows": False,
    }
    lock = Lock()

    def respond(request):
        body = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/db/neo4j/tx/commit"
        assert request.headers["Access-Mode"] == "READ"
        assert len(body["statements"]) == 1
        statement = body["statements"][0]
        assert "CREATE " not in statement["statement"]
        assert "MERGE " not in statement["statement"]
        assert "DELETE " not in statement["statement"]
        parameters = statement["parameters"]
        with lock:
            state["requests"].append(parameters)
            state["statements"].append(statement["statement"])
            number = len(state["requests"])
            mode = state["mode"]
            barrier = state["barrier"]
            neighbor_id = state["neighbor_id"]
            if state["vary_per_request"]:
                neighbor_id += "_" + str(number)
        if barrier is not None:
            barrier.wait(timeout=10)
        if mode == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC upstream timeout", request=request)
        if mode == "http_error":
            return httpx.Response(503, json={"message": "SYNTHETIC private error"})
        if mode == "protocol_error":
            return httpx.Response(200, json={
                "errors": [{"message": "SYNTHETIC private error"}], "results": [],
            })
        lookup = "name" in parameters
        columns = ["candidate"] if lookup else COLUMNS
        if state["force_empty_rows"]:
            return httpx.Response(200, json={
                "errors": [], "results": [{"columns": columns, "data": []}],
            })
        if lookup:
            candidates = state["lookup_nodes"]
            if candidates is None:
                candidates = [{**NATIVE_CANDIDATE, "name": parameters["name"].upper()}]
            return httpx.Response(200, json={
                "errors": [], "results": [{
                    "columns": columns, "data": [{"row": [node]} for node in candidates],
                }],
            })
        seed = {
            "name": "SYNTHETIC seed", "node_source": parameters["node_source"],
            "primekg_id": parameters["seed_id"], "primekg_type": "gene/protein",
        }
        neighbor = {
            "name": "SYNTHETIC neighbor " + neighbor_id,
            "node_source": "SYNTHETIC_MONDO", "primekg_id": neighbor_id,
            "primekg_type": "disease",
        }
        if mode == "missing_seed":
            row = [0, None, None, None, None, None]
        elif mode == "empty":
            row = [1, seed, None, None, None, None]
        elif mode == "ambiguous_seed":
            row = [2, None, None, None, None, None]
        else:
            # 有向端点刻意与seed顺序相反，确认发布/读取不擅自反转关系。
            row = [1, seed, neighbor, seed, "disease_protein", {
                "display_relation": "SYNTHETIC background association",
            }]
        return httpx.Response(200, json={
            "errors": [], "results": [{"columns": COLUMNS, "data": [{"row": row}]}],
        })

    def upstream_client(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)

    # 仅替换此模块的上游HTTP工厂；Starlette TestClient、业务捕获和PG均保持真实。
    monkeypatch.setattr(graph_publish, "httpx", SimpleNamespace(
        Client=upstream_client, TimeoutException=httpx.TimeoutException,
        HTTPError=httpx.HTTPError,
    ))
    app = FastAPI()
    app.include_router(router)
    app.include_router(history_graph_router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, state=state, engine=pg, app=app)


def post(api, key="synthetic-query", body=None, token=QUERY_TOKEN):
    headers = {"Idempotency-Key": key}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    return api.client.post(CAPTURE_PATH, json=QUERY if body is None else body, headers=headers)


def counts(engine):
    with engine.begin() as conn:
        return (
            conn.execute(text("SELECT count(*) FROM history_runs")).scalar_one(),
            conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one(),
            conn.execute(text(
                "SELECT published_seq FROM history_sources WHERE source_id='primekg_graph'"
            )).scalar_one(),
        )


def binding(result, *, mode="current", cutoff=None):
    ref = result["neighborhood"]["graph_ref"]
    return {
        "source_id": ref["source_id"], "source_record_id": ref["source_record_id"],
        "is_demo": "false", "mode": mode,
        "cutoff": cutoff or result["read_cutoff"],
    }


def test_query_uses_real_capture_publication_and_fixed_background_reads(graph_api):
    response = post(graph_api)
    assert response.status_code == 200, response.text
    result = response.json()
    neighborhood = result["neighborhood"]
    ref = neighborhood["graph_ref"]
    assert result["run"]["state"] == "completed"
    assert result["run"]["version_ids"] == [ref["version_id"]]
    assert result["query"] == QUERY
    assert ref["source_id"] == "primekg_graph" and ref["is_demo"] is False
    assert counts(graph_api.engine) == (1, 1, 1)
    assert len(graph_api.state["requests"]) == 1
    parameters = graph_api.state["requests"][0]
    assert parameters["seed_id"] == QUERY["seed_id"]
    assert parameters["neighbor_term"] == QUERY["neighbor_term"]
    assert parameters["relation_types"] == QUERY["relation_types"]
    assert parameters["limit_plus_one"] == QUERY["limit"] + 1
    assert all(node["identity_status"] == "background" and not node["evidence"]
               for node in neighborhood["nodes"])
    edge = neighborhood["edges"][0]
    assert edge["binding_status"] == "background" and not edge["evidence"]
    assert edge["review"]["status"] == "background"
    assert edge["source"]["namespace"] == "primekg.SYNTHETIC_MONDO"
    assert edge["target"]["namespace"] == "primekg.SYNTHETIC_NCBI"
    assert neighborhood["coverage"]["scope"] == "selected_published_subgraph"
    headers = {"Authorization": "Bearer " + READ_TOKEN}
    fixed = graph_api.client.get(
        "/api/history/versions/" + ref["version_id"], params=binding(result), headers=headers,
    )
    assert fixed.status_code == 200
    assert fixed.json()["raw_sha256"] == ref["raw_sha256"]
    bundle = json.loads(fixed.json()["raw_content"])
    assert bundle["graph_kind"] == "background" and bundle["document_refs"] == []
    raw = graph_api.client.get(
        "/api/history/versions/" + ref["version_id"] + "/raw",
        params=binding(result), headers=headers,
    )
    assert raw.status_code == 200
    assert raw.headers["x-content-sha256"] == ref["raw_sha256"]
    assert sha256(raw.text) == ref["raw_sha256"]


def test_same_key_replay_never_recaptures_and_changed_query_is_conflict(graph_api):
    first = post(graph_api).json()
    graph_api.state["mode"] = "timeout"  # 若重放误访问上游，不能成功。
    normalized = {**QUERY, "relation_types": ["drug_protein", "disease_protein", "drug_protein"]}
    replay = post(graph_api, body=normalized)
    assert replay.status_code == 200
    assert replay.json()["run"]["id"] == first["run"]["id"]
    assert replay.json()["neighborhood"] == first["neighborhood"]
    for field, value in {
        "node_type": "drug", "node_source": "OTHER_SYNTHETIC_SOURCE",
        "seed_id": "OTHER_SYNTHETIC_SEED", "relation_types": ["other_relation"],
        "neighbor_term": "OTHER", "limit": 6,
    }.items():
        conflict = post(graph_api, body={**QUERY, field: value})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert len(graph_api.state["requests"]) == 1
    assert counts(graph_api.engine) == (1, 1, 1)


def test_concurrent_same_key_adopts_one_winner_after_two_read_captures(graph_api):
    graph_api.state["barrier"] = Barrier(2)
    graph_api.state["vary_per_request"] = True

    def submit(_index):
        with TestClient(graph_api.app) as client:
            return client.post(CAPTURE_PATH, json=QUERY, headers={
                "Authorization": "Bearer " + QUERY_TOKEN,
                "Idempotency-Key": "synthetic-concurrent-query",
            })

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, [1, 2]))
    assert [response.status_code for response in responses] == [200, 200]
    results = [response.json() for response in responses]
    assert results[0]["run"]["id"] == results[1]["run"]["id"]
    assert results[0]["neighborhood"] == results[1]["neighborhood"]
    assert len(graph_api.state["requests"]) == 2
    assert counts(graph_api.engine) == (1, 1, 1)


def test_lost_ack_after_real_commit_replays_without_capturing_or_publishing_again(
    graph_api, monkeypatch
):
    actual_publish = graph_query.publish_primekg
    publications = []

    def commit_then_lose_ack(observation, key):
        run = actual_publish(observation, key)
        publications.append(run.id)
        raise RuntimeError("SYNTHETIC lost ACK after durable commit")

    monkeypatch.setattr(graph_query, "publish_primekg", commit_then_lose_ack)
    unknown = post(graph_api)
    assert unknown.status_code == 503
    assert unknown.json()["detail"]["code"] == "history_service_failed"
    assert "SYNTHETIC lost ACK" not in unknown.text
    assert counts(graph_api.engine) == (1, 1, 1)
    replay = post(graph_api)
    assert replay.status_code == 200
    assert replay.json()["run"]["id"] == publications[0]
    assert len(publications) == 1 and len(graph_api.state["requests"]) == 1
    assert counts(graph_api.engine) == (1, 1, 1)


@pytest.mark.parametrize("mode,status,code", [
    ("timeout", 504, "primekg_timeout"),
    ("http_error", 503, "primekg_read_failed"),
    ("protocol_error", 503, "primekg_query_failed"),
    ("ambiguous_seed", 422, "ambiguous_primekg_seed"),
])
def test_upstream_failure_is_not_empty_success_or_published(graph_api, mode, status, code):
    graph_api.state["mode"] = mode
    failed = post(graph_api)
    assert failed.status_code == status
    assert failed.json()["detail"]["code"] == code
    assert "SYNTHETIC private error" not in failed.text
    assert counts(graph_api.engine) == (0, 0, 0)
    graph_api.state["mode"] = "edges"
    recovered = post(graph_api)
    assert recovered.status_code == 200
    assert len(graph_api.state["requests"]) == 2
    assert counts(graph_api.engine) == (1, 1, 1)


@pytest.mark.parametrize("mode,found,missing,node_count", [
    ("empty", 1, 0, 1), ("missing_seed", 0, 1, 0),
])
def test_legal_empty_and_missing_seed_have_distinct_frozen_results(
    graph_api, mode, found, missing, node_count
):
    graph_api.state["mode"] = mode
    response = post(graph_api)
    assert response.status_code == 200
    result = response.json()
    neighborhood = result["neighborhood"]
    assert result["run"]["state"] == "completed"
    assert len(neighborhood["seeds_found"]) == found
    assert len(neighborhood["seeds_missing"]) == missing
    assert len(neighborhood["nodes"]) == node_count
    assert neighborhood["edges"] == []
    assert neighborhood["coverage"]["stop_reason"] == "end_of_published_subgraph"
    assert counts(graph_api.engine) == (1, 1, 1)
    replay = post(graph_api)
    assert replay.status_code == 200 and replay.json()["neighborhood"] == neighborhood
    assert len(graph_api.state["requests"]) == 1


@pytest.mark.parametrize("revoke", ["allowlist", "catalog"])
def test_revocation_blocks_replay_and_fixed_read_before_upstream(graph_api, monkeypatch, revoke):
    first = post(graph_api).json()
    if revoke == "allowlist":
        monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", "external_archive")
    else:
        with graph_api.engine.begin() as conn:
            conn.execute(text("UPDATE history_sources SET read_enabled=false "
                              "WHERE source_id='primekg_graph'"))
    denied = post(graph_api)
    assert denied.status_code == 403
    assert len(graph_api.state["requests"]) == 1
    ref = first["neighborhood"]["graph_ref"]
    fixed = graph_api.client.get(
        "/api/history/versions/" + ref["version_id"], params=binding(first),
        headers={"Authorization": "Bearer " + READ_TOKEN},
    )
    assert fixed.status_code == 403
    changes = graph_api.client.get(
        "/api/history/changes", params={"source_id": "primekg_graph", "is_demo": "false"},
        headers={"Authorization": "Bearer " + READ_TOKEN},
    )
    assert changes.status_code == 403 and counts(graph_api.engine) == (1, 1, 1)


@pytest.mark.parametrize("scenario", ["pending_run", "missing_version"])
def test_existing_unpublished_receipt_is_not_recaptured_or_reported_as_empty(graph_api, scenario):
    request = PrimeKGQuery(**QUERY)
    with graph_api.engine.begin() as conn:
        run, fresh = store._new_run(
            conn, "primekg_graph", False, "archive", {},
            "primekg-query:" + sha256("synthetic-query"), request.scope(),
        )
        assert fresh
        if scenario == "missing_version":
            conn.execute(text("UPDATE history_runs SET state='completed',"
                              "version_ids=CAST(:ids AS jsonb) WHERE id=:id"),
                         {"id": run["id"], "ids": json.dumps(["a" * 64])})
    response = post(graph_api)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "graph_not_published"
    assert graph_api.state["requests"] == [] and counts(graph_api.engine) == (1, 0, 0)


def test_old_fixed_version_cutoff_and_changes_keep_their_published_boundary(graph_api):
    first_response = post(graph_api, key="synthetic-first")
    assert first_response.status_code == 200
    first = first_response.json()
    graph_api.state["neighbor_id"] = "SYNTHETIC_B"
    second_response = post(graph_api, key="synthetic-second")
    assert second_response.status_code == 200
    second = second_response.json()
    first_ref, second_ref = [v["neighborhood"]["graph_ref"] for v in (first, second)]
    assert first_ref["source_record_id"] == second_ref["source_record_id"]
    assert first_ref["version_id"] != second_ref["version_id"]
    headers = {"Authorization": "Bearer " + READ_TOKEN}
    old = graph_api.client.get(
        "/api/history/versions/" + first_ref["version_id"],
        params=binding(first, mode="operational_replay"), headers=headers,
    )
    assert old.status_code == 200 and old.json()["raw_sha256"] == first_ref["raw_sha256"]
    early = datetime.fromisoformat(old.json()["first_seen_at"].replace("Z", "+00:00"))
    before = graph_api.client.get(
        "/api/history/versions/" + first_ref["version_id"],
        params=binding(first, mode="operational_replay", cutoff=(early - timedelta(microseconds=1)).isoformat()),
        headers=headers,
    )
    assert before.status_code == 404
    future_at_old_cutoff = graph_api.client.get(
        "/api/history/versions/" + second_ref["version_id"],
        params=binding(second, mode="operational_replay", cutoff=first["read_cutoff"]), headers=headers,
    )
    assert future_at_old_cutoff.status_code == 404
    replay = post(graph_api, key="synthetic-first")
    assert replay.status_code == 200 and replay.json()["neighborhood"] == first["neighborhood"]
    assert len(graph_api.state["requests"]) == 2
    changes_params = {"source_id": "primekg_graph", "is_demo": "false", "after": 0, "limit": 1}
    page = graph_api.client.get("/api/history/changes", params=changes_params, headers=headers)
    assert page.status_code == 200
    frozen = page.json()
    assert frozen["items"][0]["version_id"] == first_ref["version_id"]
    assert frozen["current_version_ids"][first_ref["source_record_id"]] == second_ref["version_id"]
    assert frozen["max_seq"] == 2 and frozen["next_cursor"]
    graph_api.state["neighbor_id"] = "SYNTHETIC_C"
    assert post(graph_api, key="synthetic-third").status_code == 200
    continued = graph_api.client.get(
        "/api/history/changes", params={**changes_params, "cursor": frozen["next_cursor"]}, headers=headers,
    )
    assert continued.status_code == 200
    assert continued.json()["max_seq"] == 2
    assert [v["version_id"] for v in continued.json()["items"]] == [second_ref["version_id"]]
    assert continued.json()["current_version_ids"][second_ref["source_record_id"]] == second_ref["version_id"]
    assert counts(graph_api.engine) == (3, 3, 3)


@pytest.mark.parametrize("token,status", [(None, 401), (READ_TOKEN, 403), (ADMIN_TOKEN, 403)])
def test_ordinary_read_and_admin_credentials_cannot_capture(graph_api, token, status):
    response = post(graph_api, token=token)
    assert response.status_code == status
    assert graph_api.state["requests"] == [] and counts(graph_api.engine) == (0, 0, 0)


def test_query_credential_cannot_import_archives_or_fetch(graph_api):
    archive = {
        "source_id": "external_archive", "is_demo": True,
        "scope_description": "explicit synthetic archive authorization check",
        "records": [{"source_record_id": "SYNTHETIC_DOC", "record_kind": "document",
                     "raw_content": json.dumps({"record_kind": "document", "title": "SYNTHETIC"})}],
    }
    headers = {"Authorization": "Bearer " + QUERY_TOKEN, "Idempotency-Key": "synthetic-forbidden-import"}
    response = graph_api.client.post("/api/history/archives", json=archive, headers=headers)
    assert response.status_code == 403 and response.json()["detail"]["code"] == "admin_required"
    response = graph_api.client.post(
        "/api/history/fetch", json={"source_id": "pubmed", "query": "SYNTHETIC"}, headers=headers,
    )
    assert response.status_code == 403 and counts(graph_api.engine) == (0, 0, 0)
    assert graph_api.state["requests"] == []


@pytest.mark.parametrize("extra", [
    {"cypher": "MATCH (n) DETACH DELETE n"},
    {"statement": "RETURN 1"},
    {"node_type": "MATCH (n)"},
    {"seed_id": "bad|identity"},
    {"limit": True},
    {"limit": 201},
    {"relation_types": ["type"] * 17},
])
def test_invalid_or_extra_cypher_requests_are_rejected_before_capture(graph_api, extra):
    response = post(graph_api, body={**QUERY, **extra})
    assert response.status_code == 422
    assert graph_api.state["requests"] == [] and counts(graph_api.engine) == (0, 0, 0)


def test_missing_query_configuration_is_not_empty_success(graph_api, monkeypatch):
    monkeypatch.delenv("DATAFABRIC_GRAPH_QUERY_TOKEN")
    response = post(graph_api)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "graph_query_not_configured"
    assert graph_api.state["requests"] == [] and counts(graph_api.engine) == (0, 0, 0)


def lookup(api, body=None, token=QUERY_TOKEN):
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    return api.client.post(LOOKUP_PATH, json=LOOKUP_QUERY if body is None else body, headers=headers)


def test_lookup_current_native_ids_are_preserved_when_passed_to_capture(graph_api):
    response = lookup(graph_api)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "resolved" and result["truncated"] is False
    assert result["query"] == LOOKUP_QUERY
    assert result["evidence_status"] == "current_identity_candidates_only"
    assert datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00")).utcoffset() is not None
    candidate = result["candidates"][0]
    assert candidate == {
        "name": NATIVE_CANDIDATE["name"], "node_type": NATIVE_CANDIDATE["primekg_type"],
        "node_source": NATIVE_CANDIDATE["node_source"], "seed_id": NATIVE_CANDIDATE["primekg_id"],
        "identity_status": "background",
    }
    assert "run" not in result and "neighborhood" not in result
    assert counts(graph_api.engine) == (0, 0, 0)  # 名称候选本身不采用。
    captured = post(graph_api, body={
        **QUERY, "node_type": candidate["node_type"],
        "node_source": candidate["node_source"], "seed_id": candidate["seed_id"],
    })
    assert captured.status_code == 200
    sent = graph_api.state["requests"][1]
    assert sent["node_source"] == "SYNTHETIC_NATIVE_NCBI"
    assert sent["seed_id"] == "00001956"  # 不变为数字/UUID，也不复用旧查询ID。
    seeds = captured.json()["neighborhood"]["seeds_found"]
    assert seeds == [{"namespace": "primekg.SYNTHETIC_NATIVE_NCBI", "id": "00001956"}]
    assert counts(graph_api.engine) == (1, 1, 1)


def test_lookup_limit_one_retains_ambiguity_before_truncating_candidates(graph_api):
    graph_api.state["lookup_nodes"] = [
        dict(NATIVE_CANDIDATE), {**NATIVE_CANDIDATE, "primekg_id": "SYNTHETIC_SECOND_ID"},
    ]
    response = lookup(graph_api, body={**LOOKUP_QUERY, "limit": 1})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ambiguous" and result["truncated"] is True
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["identity_status"] == "background"
    assert graph_api.state["requests"][0]["limit_plus_one"] == 2
    assert counts(graph_api.engine) == (0, 0, 0)


def test_lookup_duplicate_physical_candidates_are_not_silently_merged(graph_api):
    # 两个物理节点可返回完全相同的原生属性；不能据此伪称唯一身份。
    graph_api.state["lookup_nodes"] = [dict(NATIVE_CANDIDATE), dict(NATIVE_CANDIDATE)]
    response = lookup(graph_api)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ambiguous" and result["truncated"] is False
    assert len(result["candidates"]) == 2 and result["candidates"][0] == result["candidates"][1]
    assert counts(graph_api.engine) == (0, 0, 0)


def test_empty_http_rows_are_not_found_for_lookup_but_failure_for_capture(graph_api):
    graph_api.state["force_empty_rows"] = True
    response = lookup(graph_api)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "not_found" and result["candidates"] == []
    assert result["truncated"] is False
    assert counts(graph_api.engine) == (0, 0, 0)
    capture = post(graph_api)
    assert capture.status_code == 503
    assert capture.json()["detail"]["code"] == "primekg_invalid_response"
    assert counts(graph_api.engine) == (0, 0, 0)
    assert len(graph_api.state["requests"]) == 2


def test_lookup_quote_in_name_is_a_read_only_literal_parameter(graph_api):
    name = "SYNTHETIC O'Brien') MATCH (n) DETACH DELETE n //"
    response = lookup(graph_api, body={**LOOKUP_QUERY, "name": name})
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    statement = graph_api.state["statements"][0]
    assert "toLower(n.name)=toLower($name)" in statement
    assert name not in statement and "O'Brien" not in statement
    assert graph_api.state["requests"][0] == {"name": name, "limit_plus_one": 11}
    assert counts(graph_api.engine) == (0, 0, 0)


@pytest.mark.parametrize("extra", [
    {"cutoff": "2018-01-01T00:00:00Z"},
    {"cypher": "MATCH (n) DETACH DELETE n"},
    {"uri": "http://untrusted.invalid:7474"},
    {"node_type": "ANY"},
    {"name": "   "},
    {"limit": True},
    {"limit": 21},
])
def test_lookup_invalid_or_extra_inputs_are_422_before_http(graph_api, extra):
    response = lookup(graph_api, body={**LOOKUP_QUERY, **extra})
    assert response.status_code == 422
    assert graph_api.state["requests"] == [] and counts(graph_api.engine) == (0, 0, 0)


@pytest.mark.parametrize("token,status", [(None, 401), (READ_TOKEN, 403), (ADMIN_TOKEN, 403)])
def test_lookup_requires_the_independent_query_credential(graph_api, token, status):
    response = lookup(graph_api, token=token)
    assert response.status_code == status
    assert graph_api.state["requests"] == [] and counts(graph_api.engine) == (0, 0, 0)


@pytest.mark.parametrize("revoke", ["allowlist", "catalog"])
def test_lookup_rechecks_permission_before_reading_current_candidates(graph_api, monkeypatch, revoke):
    assert lookup(graph_api).status_code == 200
    if revoke == "allowlist":
        monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", "external_archive")
    else:
        with graph_api.engine.begin() as conn:
            conn.execute(text("UPDATE history_sources SET read_enabled=false "
                              "WHERE source_id='primekg_graph'"))
    denied = lookup(graph_api)
    assert denied.status_code == 403
    assert len(graph_api.state["requests"]) == 1 and counts(graph_api.engine) == (0, 0, 0)


@pytest.mark.parametrize("mode,status,code", [
    ("timeout", 504, "primekg_timeout"),
    ("http_error", 503, "primekg_read_failed"),
    ("protocol_error", 503, "primekg_query_failed"),
])
def test_lookup_failure_is_not_fake_not_found(graph_api, mode, status, code):
    graph_api.state["mode"] = mode
    failed = lookup(graph_api)
    assert failed.status_code == status
    assert failed.json()["detail"]["code"] == code
    assert "SYNTHETIC private error" not in failed.text
    assert "candidates" not in failed.json() and counts(graph_api.engine) == (0, 0, 0)
    graph_api.state["mode"] = "edges"
    assert lookup(graph_api).status_code == 200
    assert counts(graph_api.engine) == (0, 0, 0)


@pytest.mark.parametrize("field", ["name", "node_source", "primekg_id", "primekg_type"])
def test_lookup_missing_native_candidate_fields_are_503_without_publication(graph_api, field):
    incomplete = dict(NATIVE_CANDIDATE)
    incomplete.pop(field)
    graph_api.state["lookup_nodes"] = [incomplete]
    response = lookup(graph_api)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "primekg_invalid_response"
    assert counts(graph_api.engine) == (0, 0, 0)
