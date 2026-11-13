"""合成SEC HTTP输入经过真实隔离PG；不代表真实企业数据，不访问SEC/生产库。"""

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from integration_history import pg, pg_engine  # noqa: F401 -- shared isolated PG fixtures
from test_sec_xbrl import NATIVE, value

from api.routers.history import router
from fetch import sec_xbrl
from fetch.sources import read_public
from store import history
from store.history_models import FetchRequest


def test_sec_real_fetch_adoption_dedup_restore_and_fixed_row_locators(pg, monkeypatch):
    state = {"data": value(), "calls": 0}
    real = httpx.Client

    def respond(request):
        state["calls"] += 1
        return httpx.Response(200, text=json.dumps(state["data"], indent=2))

    monkeypatch.setattr(sec_xbrl, "httpx", SimpleNamespace(
        Client=lambda **kwargs: real(transport=httpx.MockTransport(respond), **kwargs),
        HTTPError=httpx.HTTPError,
    ))
    request = FetchRequest(source_id="sec_companyfacts", query="*", source_record_ids=[NATIVE])

    def adopt(key):
        run, fresh = history.create_fetch(request, key)
        assert fresh
        history.job_input(run.id, run.attempt)
        return history.finish_fetch(run.id, run.attempt, read_public(request))

    before = datetime.now(timezone.utc)
    a = adopt("synthetic-sec-a")
    repeated = adopt("synthetic-sec-same-a")
    assert a.version_ids == repeated.version_ids
    assert repeated.coverage["new_version_count"] == 0
    state["data"] = copy.deepcopy(value())
    state["data"]["units"]["USD"][0]["val"] = -30
    b = adopt("synthetic-sec-b")
    state["data"] = value()
    restored = adopt("synthetic-sec-restored-a")
    assert a.version_ids[0] != b.version_ids[0] != restored.version_ids[0]
    assert restored.version_ids[0] != a.version_ids[0]
    assert state["calls"] == 4
    cutoff = datetime.now(timezone.utc)
    rows = [history.read_version(v.version_ids[0], "sec_companyfacts", NATIVE, False, "current", cutoff)
            for v in (a, b, restored)]
    assert rows[0].raw_content == rows[2].raw_content
    assert [r.facts["financial_observations"][0]["val"] for r in rows] == [-25, -30, -25]
    assert [r.published_seq for r in rows] == [1, 2, 3]
    assert all(r.first_seen_at >= before for r in rows)
    assert all(r.source_published_at is None and r.availability_proof.kind == "unverified" for r in rows)
    current = history.list_records("sec_companyfacts", False, "current", cutoff)
    assert [r.version_id for r in current.items] == restored.version_ids
    assert len(history.list_changes("sec_companyfacts", False)["items"]) == 3
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/history/versions/" + a.version_ids[0], params={
            "source_id": "sec_companyfacts", "source_record_id": NATIVE,
            "is_demo": "false", "mode": "current", "cutoff": cutoff.isoformat(),
        }, headers={"Authorization": "Bearer test-reader-only"})
        assert response.status_code == 200 and response.json()["raw_sha256"] == rows[0].raw_sha256
