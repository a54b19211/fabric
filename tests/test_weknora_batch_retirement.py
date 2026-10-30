"""The retired daemon endpoint cannot acknowledge work that will disappear."""
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import weknora


def test_batch_sync_is_explicitly_retired_before_any_io(monkeypatch):
    pending = Mock(return_value=([{"uuid": "doc"}], 1))
    client = Mock()
    worker = Mock()
    monkeypatch.setattr(weknora, "get_pending_weknora_sync", pending)
    monkeypatch.setattr(weknora, "get_weknora_graph_client", client)
    monkeypatch.setattr(weknora, "threading", SimpleNamespace(Thread=worker), raising=False)
    app = FastAPI()
    app.include_router(weknora.router)
    response = TestClient(app).post("/api/weknora/sync-batch?kb_id=old", json={"limit": 1})
    assert response.status_code == 410
    detail = response.json()["detail"]
    assert detail["code"] == "legacy_batch_sync_retired"
    assert detail["publisher"] == "python -m flows.history_publish"
    assert detail["status_url"] == "/api/history/index/status"
    assert detail["retry_url"] == "/api/history/index/{publication_id}/retry"
    pending.assert_not_called()
    client.assert_not_called()
    worker.assert_not_called()
    operation = app.openapi()["paths"]["/api/weknora/sync-batch"]["post"]
    assert operation["deprecated"] is True
    assert "410" in operation["responses"]
