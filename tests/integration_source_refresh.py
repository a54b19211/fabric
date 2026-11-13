"""定时刷新在真实 PostgreSQL 上（两个临时 schema：采集库的 publication_outbox、history 库）：一段一段读完一个周期、
出错重读、锁住的 watch 跳过、改造前的状态行接着跑、/source-health 给雷达的键。队列换成桩，不联网。"""

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_source_refresh import who_value

from fetch import fetch_job, source_refresh
from graph.db.models import Base
from graph.publish import publication
from store import history
from store.history_models import ArchiveRecord, HistoryError
from store.migrations import runner

STATE = "SELECT event_key, snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'"


@pytest.fixture
def dbs(monkeypatch):
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("needs the throwaway PostgreSQL of ops/run_integration_tests.sh")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and (parsed.port, parsed.database) == (18436, "fabric_history")
    admin = create_engine(url)
    names = ["t_refresh_outbox_" + uuid4().hex[:12], "t_refresh_history_" + uuid4().hex[:12]]
    with admin.begin() as conn:
        for name in names:
            conn.execute(text("CREATE SCHEMA " + name))
    # the outbox schema also sees public (the vector extension of the graph tables), as in integration_publication
    outbox, target = (create_engine(url, connect_args={"options": "-csearch_path=" + name + extra})
                      for name, extra in zip(names, (",public", "")))
    monkeypatch.setattr(publication, "publication_engine", lambda: outbox)
    monkeypatch.setattr(history, "history_engine", lambda: target)
    monkeypatch.setattr(fetch_job, "enqueue_fetch", lambda run: run)  # the worker is played by finish()
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_MIN_FREE_BYTES", "0")
    Base.metadata.create_all(outbox)
    runner.apply("publication", outbox)
    runner.apply("history", target)
    try:
        yield outbox, target
    finally:
        outbox.dispose()
        target.dispose()
        with admin.begin() as conn:
            for name in names:
                conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
        admin.dispose()


def rows(engine, sql, **params):
    with engine.begin() as conn:
        return list(conn.execute(text(sql), params).mappings())


def watch(monkeypatch, source="who_don", query="*"):
    config = [{"id": "synthetic-watch", "request": {"source_id": source, "query": query, "max_records": 1},
               "cadence_seconds": 3600, "lookback_days": 30}]
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps(config))


def state(outbox):
    return rows(outbox, STATE)[0]["snapshot"]


def payloads(target):
    return [r["request_payload"] for r in rows(target, "SELECT request_payload FROM history_runs ORDER BY created_at")]


def finish(outbox, records=(), continuation=None, exhausted=False, error=None, stop=None):
    """The fetch worker's part: the watch's active run is read and stored."""
    run = history.read_run(state(outbox)["active_run_id"])
    history.job_input(run.id, run.attempt)
    stop = stop or ("upstream_exhausted" if exhausted else "record_budget")
    return history.finish_fetch(run.id, run.attempt, {
        "records": list(records), "complete": False, "upstream_pagination_exhausted": exhausted, "stop_reason": stop,
        "error": error, "requests": 1, "bytes": 100, "source_total": None, "next_continuation": continuation})


def make_due(outbox):  # the cadence has passed
    with outbox.begin() as conn:
        conn.execute(text("UPDATE publication_outbox SET snapshot = jsonb_set(snapshot, '{next_check_at}', "
                          "to_jsonb('2000-01-01T00:00:00+00:00'::text)) WHERE event_key LIKE 'source-watch:%'"))


def who_record(number):
    value = who_value(number)
    return ArchiveRecord(source_record_id=value["Id"], record_kind="document", raw_content=json.dumps(value))


def test_a_cycle_is_read_segment_by_segment_then_waits_for_its_cadence(dbs, monkeypatch):
    outbox, target = dbs
    watch(monkeypatch)
    assert source_refresh.refresh_once() == [{"watch_id": "synthetic-watch", "state": "running"}]
    first = state(outbox)
    assert first["request"]["updated_until"] == first["window_ended_at"]  # WHO: the update window of this cycle
    assert source_refresh.refresh_once() == [{"watch_id": "synthetic-watch", "state": "running"}]  # still queued
    assert len(payloads(target)) == 1
    cursor = {"cursor": json.dumps({"modified": "2026-10-11T12:00:01Z", "id": who_value(1)["Id"]}), "read_count": 1}
    finish(outbox, [who_record(1)], continuation=cursor)
    source_refresh.refresh_once()  # the segment is done
    source_refresh.refresh_once()  # the next round starts the next one, at its cursor, in the same window
    second = payloads(target)[1]
    assert second["continuation"] == cursor and second["updated_until"] == payloads(target)[0]["updated_until"]
    finish(outbox, [who_record(2)], exhausted=True)
    source_refresh.refresh_once()
    done = state(outbox)
    assert done["state"] == "idle" and done["last_success_at"] and "request" not in done
    assert done["window_checkpoint"] == first["window_ended_at"]  # the next cycle reads on from here
    assert source_refresh.refresh_once() == []  # waits for its cadence
    assert rows(target, "SELECT count(*) AS n FROM history_versions")[0]["n"] == 2


def test_a_failed_segment_is_read_again_without_stopping_at_stored_records(dbs, monkeypatch):
    """nmpa has no update window: the cycle reads the list from its top and stops at known_stop stored records. A
    segment that failed is read again in full: stopping at the records the failed try stored would skip the rest."""
    outbox, target = dbs
    watch(monkeypatch, source="nmpa", query="https://www.nmpa.gov.cn/xxgk/ggtg/index.html")
    source_refresh.refresh_once()
    assert payloads(target)[0]["known_stop"] == 3 and "updated_since" not in payloads(target)[0]
    finish(outbox, error={"code": "source_read_failed", "message": "SYNTHETIC failure"}, stop="source_failed")
    source_refresh.refresh_once()
    failed = state(outbox)
    assert failed["state"] == "failed" and failed["error"]["code"] == "source_watch_failed"
    assert "active_run_id" not in failed and "last_success_at" not in failed
    assert source_refresh.refresh_once() == []  # waits for its cadence
    make_due(outbox)
    source_refresh.refresh_once()
    assert len(payloads(target)) == 2 and payloads(target)[1]["known_stop"] == 0
    finish(outbox, stop="reached_known")
    source_refresh.refresh_once()
    assert state(outbox)["state"] == "idle" and state(outbox)["error"] is None


def test_a_watch_locked_by_another_scheduler_is_skipped(dbs, monkeypatch):
    outbox, target = dbs
    watch(monkeypatch)
    key = source_refresh._key(source_refresh.watches()[0])
    with outbox.begin() as conn:
        conn.execute(text("INSERT INTO publication_outbox (event_key, kind, source_doc_uuid, snapshot) "
                          "VALUES (:key, 'control', :key, '{}')"), {"key": key})
    with outbox.begin() as other:  # another scheduler is in the middle of this watch's step
        other.execute(text("SELECT 1 FROM publication_outbox WHERE event_key = :key FOR UPDATE"), {"key": key})
        assert source_refresh.refresh_once() == []
    assert payloads(target) == []
    assert source_refresh.refresh_once()[0]["state"] == "running" and len(payloads(target)) == 1


def test_a_state_row_written_before_the_rewrite_goes_on(dbs, monkeypatch):
    """The old scheduler's row (lease fields, request_key, a continuation with its scope_hash, no known_stop): the
    segment it was on is read on, from its cursor, in its window; the old fields do no harm."""
    outbox, target = dbs
    watch(monkeypatch)
    key = source_refresh._key(source_refresh.watches()[0])
    request = {"source_id": "who_don", "query": "*", "source_record_ids": [], "max_records": 1, "max_pages": 3,
               "deadline_seconds": 60, "updated_since": "2026-10-01T00:00:00+00:00",
               "updated_until": "2026-10-31T00:00:00+00:00",
               "continuation": {"scope_hash": "0" * 64, "cursor": "SYNTHETIC-CURSOR", "read_count": 7}}
    old = {"state": "paging", "request": request, "generation": 4, "request_key": key + ":4", "cycle_pages": 2,
           "lease_until": "2999-01-01T00:00:00+00:00", "lease_token": "old-owner", "watch_id": "synthetic-watch",
           "window_ended_at": "2026-10-31T00:00:00+00:00", "next_check_at": "2026-10-31T00:00:00+00:00"}
    with outbox.begin() as conn:
        conn.execute(text("INSERT INTO publication_outbox (event_key, kind, source_doc_uuid, snapshot) "
                          "VALUES (:key, 'control', :key, CAST(:old AS jsonb))"), {"key": key, "old": json.dumps(old)})
    assert source_refresh.refresh_once()[0]["state"] == "running"
    [payload] = payloads(target)
    assert payload["continuation"]["cursor"] == "SYNTHETIC-CURSOR" and payload["updated_until"].startswith("2026-10-31")


def test_source_health_gives_the_radar_its_keys_and_refuses_a_closed_source(dbs, monkeypatch):
    outbox, target = dbs
    watch(monkeypatch)
    health = source_refresh.source_health("who_don")
    assert health["collection"]["state"] == "unknown" and health["watches"][0]["state"] is None  # not run yet
    source_refresh.refresh_once()
    finish(outbox, error={"code": "source_read_failed", "message": "SYNTHETIC failure"}, stop="source_failed")
    source_refresh.refresh_once()
    health = source_refresh.source_health("who_don")
    assert set(health["collection"]) == {"state", "last_recorded_success_at", "last_recorded_failure_at", "scope",
                                         "coverage"}
    assert health["collection"]["state"] == "failed" and health["collection"]["last_recorded_failure_at"]
    [row] = health["watches"]
    assert row["watch_id"] == "synthetic-watch" and row["scope"] == "*" and row["state"] == "failed"
    assert row["window_ended_at"] == state(outbox)["window_ended_at"]
    assert source_refresh.source_health("nmpa")["watches"] == []  # a source without a watch
    with target.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled = false WHERE source_id = 'who_don'"))
    with pytest.raises(HistoryError) as exc:
        source_refresh.source_health("who_don")
    assert exc.value.status == 403
