"""DATAFABRIC_FETCH_ENABLED=false: no new fetch task, and a task already queued does not go out either."""
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import fabric as api
from fetch import fetch_job as worker
from fetch import sources
from store.record_models import FabricError, FetchRequest

FLAG = "DATAFABRIC_FETCH_ENABLED"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_ADMIN_TOKEN", "mock-admin")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app, headers={"Authorization": "Bearer mock-admin"})


@pytest.mark.parametrize("value", ["false", "0", "", "yes"])
def test_a_closed_instance_creates_no_fetch_task(monkeypatch, client, value):
    monkeypatch.setenv(FLAG, value)
    create = Mock(side_effect=AssertionError("must not create a run"))
    monkeypatch.setattr(api.store, "create_fetch", create)
    response = client.post("/api/fabric/fetch", json={"source_id": "clinicaltrials", "query": "*"})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "public_fetch_disabled"
    create.assert_not_called()


def test_a_task_queued_before_the_closure_does_not_go_out(monkeypatch):
    monkeypatch.setenv(FLAG, "false")
    monkeypatch.setattr(worker.records, "job_input", Mock(return_value=FetchRequest(source_id="clinicaltrials", query="*")))
    finish, failure = Mock(), Mock()
    monkeypatch.setattr(worker.records, "finish_fetch", finish)
    monkeypatch.setattr(worker.records, "fail_job", failure)
    monkeypatch.setattr(sources.httpx, "Client", Mock(side_effect=AssertionError("HTTP forbidden")))
    with pytest.raises(FabricError) as error:
        worker.run_fetch_job("queued", 1)
    assert error.value.code == "public_fetch_disabled"
    finish.assert_not_called()
    assert failure.call_args.args[:3] == ("queued", 1, "public_fetch_disabled")
