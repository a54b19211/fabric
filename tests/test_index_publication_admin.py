"""索引发布管理入口（列表/计数/重试）：伪连接记录 SQL，不连真库。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import history as history_router
from store import history
from store import index_publication as index
from store.history_models import HistoryError

ADMIN = "synthetic-admin-token"
JOB = "a" * 64
STAMP = datetime(2026, 11, 4, 8, 0, 0, 123456, tzinfo=timezone.utc)


class Result:
    def __init__(self, rows=(), rowcount=0):
        self.rows, self.rowcount = list(rows), rowcount

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class Conn:
    def __init__(self, results):
        self.results, self.queries = list(results), []

    def execute(self, statement, params=None):
        self.queries.append((str(statement), dict(params or {})))
        return self.results.pop(0)


@pytest.fixture
def db(monkeypatch):
    holder = {"conn": Conn([])}

    class Engine:
        @contextmanager
        def begin(self):
            yield holder["conn"]

    monkeypatch.setattr(history, "history_engine", lambda: Engine())
    monkeypatch.setattr(history, "_source", lambda conn, source_id: None)

    def script(*results):
        holder["conn"] = Conn(results)
        return holder["conn"]

    return script


def job_row(n, **extra):
    return {
        "id": format(n, "064x"), "target_id": "t" * 64, "version_id": "v" * 64, "source_record_id": "NCT%08d" % n,
        "remote_id": "r" * 64, "state": "blocked", "desired_state": "active", "knowledge_id": None, "receipt": {},
        "error": {"code": "index_unavailable", "message": "索引处理暂不可用"}, "attempts": 6,
        "first_send_at": None, "next_check_at": STAMP, "updated_at": STAMP - timedelta(seconds=n),
        "artifact_key": "", "manifest": {"title": "Synthetic %d" % n},
        "config": {"source_id": "clinicaltrials", "is_demo": True, "knowledge_base_id": "kb", "source_scope": "s",
                   "source_instance": "i"},
    } | extra


def test_status_filters_pages_and_shows_errors(db):
    conn = db(Result([job_row(1), job_row(2), job_row(3)]))
    cursor = STAMP.isoformat() + "|" + JOB
    page = index.status(limit=2, state="blocked", source_id="clinicaltrials", cursor=cursor)
    sql, params = conn.queries[0]
    assert "p.state=:state" in sql and "t.config->>'source_id'=:source" in sql
    assert "(p.updated_at,p.id)<(:before_at,:before_id)" in sql
    assert "ORDER BY p.updated_at DESC,p.id DESC LIMIT :limit" in sql
    assert params == {"limit": 3, "state": "blocked", "source": "clinicaltrials",
                      "before_at": STAMP, "before_id": JOB}
    assert [item["source_record_id"] for item in page["items"]] == ["NCT00000001", "NCT00000002"]
    first = page["items"][0]
    assert first["error"]["code"] == "index_unavailable" and first["attempts"] == 6
    assert first["title"] == "Synthetic 1" and first["read_permitted"] is True and "manifest" not in first
    last = page["items"][-1]
    assert page["next_cursor"] == last["updated_at"].isoformat() + "|" + last["id"]
    assert index._cursor(page["next_cursor"]) == (last["updated_at"], last["id"])


def test_status_last_page_has_no_cursor_and_flags_revoked_sources(db, monkeypatch):
    conn = db(Result([job_row(1)]))

    def revoked(conn, source_id):
        raise HistoryError("source_forbidden", "来源未授权", 403)

    monkeypatch.setattr(history, "_source", revoked)
    page = index.status()
    assert page["next_cursor"] is None and page["items"][0]["read_permitted"] is False
    sql, params = conn.queries[0]
    assert "WHERE" not in sql and params == {"limit": 101}


@pytest.mark.parametrize("kwargs, code", [
    ({"limit": 0}, "index_budget"), ({"limit": 101}, "index_budget"),
    ({"state": "failed"}, "index_filter"), ({"source_id": ""}, "index_filter"),
    ({"cursor": "garbage"}, "index_cursor"), ({"cursor": "2026-11-04T08:00:00|" + JOB}, "index_cursor"),
    ({"cursor": STAMP.isoformat() + "|" + "A" * 64}, "index_cursor"),
])
def test_invalid_status_requests_stop_before_database(db, kwargs, code):
    conn = db()
    with pytest.raises(HistoryError) as caught:
        index.status(**kwargs)
    assert caught.value.code == code and conn.queries == []


def test_retry_schedules_only_blocked_jobs(db):
    conn = db(Result(rowcount=1))
    assert index.retry(JOB) == {"id": JOB, "scheduled": True, "state": "waiting"}
    sql, params = conn.queries[0]
    assert "state='blocked'" in sql and "attempts=0" in sql and "updated_at=clock_timestamp()" in sql
    assert params == {"id": JOB} and len(conn.queries) == 1


def test_retry_reports_why_nothing_was_scheduled(db):
    db(Result(rowcount=0), Result([{"state": "ready"}]))
    assert index.retry(JOB) == {"id": JOB, "scheduled": False, "state": "ready", "reason": "not_blocked"}


def test_retry_unknown_or_malformed_id(db):
    db(Result(rowcount=0), Result([]))
    with pytest.raises(HistoryError) as caught:
        index.retry(JOB)
    assert caught.value.status == 404
    conn = db()
    with pytest.raises(HistoryError) as caught:
        index.retry("../" + JOB)
    assert caught.value.status == 422 and conn.queries == []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("DATAFABRIC_HISTORY_READ_TOKEN", "synthetic-read-token")
    app = FastAPI()
    app.include_router(history_router.router)
    with TestClient(app) as test_client:
        yield test_client


def test_index_routes_require_admin(client):
    for method, path in [("get", "/api/history/index/status"), ("get", "/api/history/index/counts"),
                         ("post", "/api/history/index/" + JOB + "/retry")]:
        assert getattr(client, method)(path).status_code == 401
        denied = getattr(client, method)(path, headers={"Authorization": "Bearer synthetic-read-token"})
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "admin_required"


def test_index_routes_pass_filters_fill_counts_and_map_errors(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(index, "status", lambda **kw: seen.update(kw) or {"items": [], "next_cursor": None})
    monkeypatch.setattr(index, "state_counts", lambda: {"blocked": 2, "ready": 5})
    headers = {"Authorization": "Bearer " + ADMIN}
    listed = client.get("/api/history/index/status", headers=headers,
                        params={"state": "blocked", "source_id": "who_don", "limit": 10})
    assert listed.status_code == 200 and listed.json() == {"items": [], "next_cursor": None}
    assert seen == {"limit": 10, "state": "blocked", "source_id": "who_don", "cursor": None}
    assert client.get("/api/history/index/status", headers=headers, params={"limit": 101}).status_code == 422
    counts = client.get("/api/history/index/counts", headers=headers).json()
    assert counts["total"] == 7 and list(counts["states"]) == list(index.STATES)
    assert counts["states"]["blocked"] == 2 and counts["states"]["queued"] == 0

    def missing(key):
        raise HistoryError("index_publication_not_found", "没有这条索引作业", 404)

    monkeypatch.setattr(index, "retry", missing)
    retried = client.post("/api/history/index/" + JOB + "/retry", headers=headers)
    assert retried.status_code == 404 and retried.json()["detail"]["code"] == "index_publication_not_found"
