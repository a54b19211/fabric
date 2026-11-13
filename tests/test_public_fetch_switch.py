"""Runtime closure prevents new API work and HTTP from already queued workers."""
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import history as api
from fetch import fetch_job as worker
from fetch import sources
from store.history_models import FetchRequest, HistoryError, RunView

FLAG = "DATAFABRIC_HISTORY_FETCH_ENABLED"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_ADMIN_TOKEN", "mock-admin")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app, headers={"Authorization": "Bearer mock-admin", "Idempotency-Key": "mock-key"})


def queued_run():
    return RunView(id="run", source_id="clinicaltrials", is_demo=False, kind="fetch", state="queued",
                   attempt=1, created_at="2026-10-31T00:00:00Z", scope_description="bounded test",
                   version_ids=[], coverage={}, attempts=[])


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "", "yes", "on", "invalid"])
def test_disabled_api_rejects_before_persistence_queue_or_retry(monkeypatch, client, value):
    monkeypatch.setenv(FLAG, value)
    create = Mock(side_effect=AssertionError("must not create a run"))
    enqueue = Mock(side_effect=AssertionError("must not enqueue"))
    retry = Mock(side_effect=AssertionError("must not touch queue or retry attempt"))
    monkeypatch.setattr(api.store, "create_fetch", create)
    monkeypatch.setattr(worker, "enqueue_fetch", enqueue)
    monkeypatch.setattr(worker, "retry_inactive", retry)
    for url, body in [("/api/history/fetch", {"source_id": "clinicaltrials", "query": "*"}),
                      ("/api/history/runs/existing/retry", {"expected_attempt": 1})]:
        response = client.post(url, json=body)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "public_fetch_disabled"
    create.assert_not_called()
    enqueue.assert_not_called()
    retry.assert_not_called()


@pytest.mark.parametrize("source", ["clinicaltrials", "pubmed", "fda_crl", "europe_pmc",
                                    "who_don", "sec_companyfacts", "gdelt_doc"])
def test_disabled_public_reader_blocks_every_source_before_http_or_dispatch(monkeypatch, source):
    from fetch import gdelt_news, sec_xbrl, who_don

    monkeypatch.setenv(FLAG, "false")
    http = Mock(side_effect=AssertionError("HTTP forbidden"))
    delegated = Mock(side_effect=AssertionError("source dispatch forbidden"))
    monkeypatch.setattr(sources.httpx, "Client", http)
    for module, name in [(gdelt_news, "read_gdelt"), (who_don, "read_who"), (sec_xbrl, "read_sec")]:
        monkeypatch.setattr(module, name, delegated)
    with pytest.raises(HistoryError) as error:
        sources.read_public(FetchRequest(source_id=source, query="*"))
    assert error.value.code == "public_fetch_disabled"
    assert error.value.status == 403
    http.assert_not_called()
    delegated.assert_not_called()


def test_already_queued_worker_cannot_fetch_after_runtime_closure(monkeypatch):
    monkeypatch.setenv(FLAG, "false")
    monkeypatch.setattr(worker, "job_input", Mock(return_value=(FetchRequest(source_id="clinicaltrials", query="*"), None)))
    finish, failure, http = Mock(), Mock(), Mock(side_effect=AssertionError("HTTP forbidden"))
    monkeypatch.setattr(worker, "finish_fetch", finish)
    monkeypatch.setattr(worker, "fail_job", failure)
    monkeypatch.setattr(sources.httpx, "Client", http)
    with pytest.raises(HistoryError) as error:
        worker.history_fetch_job("already-queued", 2)
    assert error.value.code == "public_fetch_disabled"
    finish.assert_not_called()
    http.assert_not_called()
    assert failure.call_args.args[:3] == ("already-queued", 2, "public_fetch_disabled")


@pytest.mark.parametrize("value", [None, "true"])
def test_default_and_explicit_true_preserve_fetch_and_retry(monkeypatch, client, value):
    monkeypatch.delenv(FLAG, raising=False)
    if value is not None:
        monkeypatch.setenv(FLAG, value)
    run = queued_run()
    create, enqueue, retry = Mock(return_value=(run, True)), Mock(return_value=run), Mock(return_value=run)
    monkeypatch.setattr(api.store, "create_fetch", create)
    monkeypatch.setattr(worker, "enqueue_fetch", enqueue)
    monkeypatch.setattr(worker, "retry_inactive", retry)
    assert client.post("/api/history/fetch", json={"source_id": "clinicaltrials", "query": "*"}).status_code == 200
    assert client.post("/api/history/runs/run/retry", json={"expected_attempt": 1}).status_code == 200
    assert create.call_count == enqueue.call_count == retry.call_count == 1


@pytest.mark.parametrize("value", [None, "true"])
def test_default_and_explicit_true_keep_the_existing_reader(monkeypatch, value):
    monkeypatch.delenv(FLAG, raising=False)
    if value is not None:
        monkeypatch.setenv(FLAG, value)
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"studies": [], "totalCount": 0})
    original = httpx.Client
    monkeypatch.setattr(sources.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(response), **kw))
    result = sources.read_public(FetchRequest(source_id="clinicaltrials", query="*"))
    assert result["upstream_pagination_exhausted"] is True
    assert result["error"] is None
    assert len(calls) == 1


def test_closure_does_not_block_archive_import_or_fixed_version_read(monkeypatch, client):
    monkeypatch.setenv(FLAG, "false")
    run = queued_run().model_copy(update={"kind": "archive"})
    archive = Mock(return_value=run)
    monkeypatch.setattr(api.store, "import_archive", archive)
    response = client.post("/api/history/archives", json={"source_id": "external_archive", "is_demo": True,
        "scope_description": "fixed archive", "declared_count": 0, "records": []})
    assert response.status_code == 200
    archive.assert_called_once()
    fixture = json.loads((Path(__file__).parent / "fixtures/native_graph_v1.json").read_text())["record"]
    version = Mock(return_value=fixture)
    monkeypatch.setattr(api.store, "read_version", version)
    response = client.get("/api/history/versions/" + fixture["version_id"], params={
        "source_id": fixture["source_id"], "source_record_id": fixture["source_record_id"],
        "is_demo": True, "mode": "current", "cutoff": "2026-10-31T00:00:00Z"})
    assert response.status_code == 200
    assert response.json()["raw_content"] == fixture["raw_content"]
    version.assert_called_once()
