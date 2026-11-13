"""定时刷新在真实 PostgreSQL 上（一个临时 schema 当 History 库）：一段一段读完一个周期、出错重读、锁住的 watch 跳过、
/source-health 给雷达的键。只用 History 库（watch 状态在 history_source_watches），队列换成桩，不联网。"""

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_source_refresh import who_value

from fetch import fetch_job, source_refresh
from store import history
from store.history_models import ArchiveRecord, HistoryError
from store.migrations import runner

STATE = "SELECT watch_key, state FROM history_source_watches"


@pytest.fixture
def target(monkeypatch):
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("needs the throwaway PostgreSQL of ops/run_integration_tests.sh")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and (parsed.port, parsed.database) == (18436, "fabric_history")
    admin = create_engine(url)
    name = "t_refresh_history_" + uuid4().hex[:12]
    with admin.begin() as conn:
        conn.execute(text("CREATE SCHEMA " + name))
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + name})
    monkeypatch.setattr(history, "history_engine", lambda: engine)
    monkeypatch.setattr(fetch_job, "enqueue_fetch", lambda run: run)  # the worker is played by finish()
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_MIN_FREE_BYTES", "0")
    monkeypatch.delenv("DATAFABRIC_PUBLICATION_DATABASE_URL", raising=False)  # fetching needs no graph database
    runner.apply("history", engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
        admin.dispose()


def rows(engine, sql, **params):
    with engine.begin() as conn:
        return list(conn.execute(text(sql), params).mappings())


def watch(monkeypatch, source="who_don", query="*"):
    config = [{"id": "synthetic-watch", "request": {"source_id": source, "query": query, "max_records": 1},
               "cadence_seconds": 3600, "lookback_days": 30}]
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps(config))


def state(target):
    return rows(target, STATE)[0]["state"]


def payloads(target):
    return [r["request_payload"] for r in rows(target, "SELECT request_payload FROM history_runs ORDER BY created_at")]


def finish(target, records=(), continuation=None, exhausted=False, error=None, stop=None):
    """The fetch worker's part: the watch's active run is read and stored."""
    run = history.read_run(state(target)["active_run_id"])
    history.job_input(run.id, run.attempt)
    stop = stop or ("upstream_exhausted" if exhausted else "record_budget")
    return history.finish_fetch(run.id, run.attempt, {
        "records": list(records), "complete": False, "upstream_pagination_exhausted": exhausted, "stop_reason": stop,
        "error": error, "requests": 1, "bytes": 100, "source_total": None, "next_continuation": continuation})


def make_due(target):  # the cadence has passed
    with target.begin() as conn:
        conn.execute(text("UPDATE history_source_watches SET state = jsonb_set(state, '{next_check_at}', "
                          "to_jsonb('2000-01-01T00:00:00+00:00'::text))"))


def who_record(number):
    value = who_value(number)
    return ArchiveRecord(source_record_id=value["Id"], record_kind="document", raw_content=json.dumps(value))


def test_a_cycle_is_read_segment_by_segment_then_waits_for_its_cadence(target, monkeypatch):
    watch(monkeypatch)
    assert source_refresh.refresh_once() == [{"watch_id": "synthetic-watch", "state": "running"}]
    first = state(target)
    assert first["request"]["updated_until"] == first["window_ended_at"]  # WHO: the update window of this cycle
    assert source_refresh.refresh_once() == [{"watch_id": "synthetic-watch", "state": "running"}]  # still queued
    assert len(payloads(target)) == 1
    cursor = {"cursor": json.dumps({"modified": "2026-10-11T12:00:01Z", "id": who_value(1)["Id"]}), "read_count": 1}
    finish(target, [who_record(1)], continuation=cursor)
    source_refresh.refresh_once()  # the segment is done
    source_refresh.refresh_once()  # the next round starts the next one, at its cursor, in the same window
    second = payloads(target)[1]
    assert second["continuation"] == cursor and second["updated_until"] == payloads(target)[0]["updated_until"]
    finish(target, [who_record(2)], exhausted=True)
    source_refresh.refresh_once()
    done = state(target)
    assert done["state"] == "idle" and done["last_success_at"] and "request" not in done
    assert done["window_checkpoint"] == first["window_ended_at"]  # the next cycle reads on from here
    assert source_refresh.refresh_once() == []  # waits for its cadence
    assert rows(target, "SELECT count(*) AS n FROM history_versions")[0]["n"] == 2


def test_a_failed_segment_is_read_again_without_stopping_at_stored_records(target, monkeypatch):
    """nmpa has no update window: the cycle reads the list from its top and stops at known_stop stored records. A
    segment that failed is read again in full: stopping at the records the failed try stored would skip the rest."""
    watch(monkeypatch, source="nmpa", query="https://www.nmpa.gov.cn/xxgk/ggtg/index.html")
    source_refresh.refresh_once()
    assert payloads(target)[0]["known_stop"] == 3 and "updated_since" not in payloads(target)[0]
    finish(target, error={"code": "source_read_failed", "message": "SYNTHETIC failure"}, stop="source_failed")
    source_refresh.refresh_once()
    failed = state(target)
    assert failed["state"] == "failed" and failed["error"]["code"] == "source_watch_failed"
    assert "active_run_id" not in failed and "last_success_at" not in failed
    assert source_refresh.refresh_once() == []  # waits for its cadence
    make_due(target)
    source_refresh.refresh_once()
    assert len(payloads(target)) == 2 and payloads(target)[1]["known_stop"] == 0
    finish(target, stop="reached_known")
    source_refresh.refresh_once()
    assert state(target)["state"] == "idle" and state(target)["error"] is None


def test_a_watch_locked_by_another_scheduler_is_skipped(target, monkeypatch):
    watch(monkeypatch)
    key = source_refresh._key(source_refresh.watches()[0])
    with target.begin() as conn:
        conn.execute(text("INSERT INTO history_source_watches (watch_key, watch_id) VALUES (:key, 'synthetic-watch')"),
                     {"key": key})
    with target.begin() as other:  # another scheduler is in the middle of this watch's step
        other.execute(text("SELECT 1 FROM history_source_watches WHERE watch_key = :key FOR UPDATE"), {"key": key})
        assert source_refresh.refresh_once() == []
    assert payloads(target) == []
    assert source_refresh.refresh_once()[0]["state"] == "running" and len(payloads(target)) == 1


def test_source_health_gives_the_radar_its_keys_and_refuses_a_closed_source(target, monkeypatch):
    watch(monkeypatch)
    health = source_refresh.source_health("who_don")
    assert health["collection"]["state"] == "unknown" and health["watches"][0]["state"] is None  # not run yet
    source_refresh.refresh_once()
    finish(target, error={"code": "source_read_failed", "message": "SYNTHETIC failure"}, stop="source_failed")
    source_refresh.refresh_once()
    health = source_refresh.source_health("who_don")
    assert set(health["collection"]) == {"state", "last_recorded_success_at", "last_recorded_failure_at", "scope",
                                         "coverage"}
    assert health["collection"]["state"] == "failed" and health["collection"]["last_recorded_failure_at"]
    [row] = health["watches"]
    assert row["watch_id"] == "synthetic-watch" and row["scope"] == "*" and row["state"] == "failed"
    assert row["window_ended_at"] == state(target)["window_ended_at"]
    assert source_refresh.source_health("nmpa")["watches"] == []  # a source without a watch
    with target.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled = false WHERE source_id = 'who_don'"))
    with pytest.raises(HistoryError) as exc:
        source_refresh.source_health("who_don")
    assert exc.value.status == 403
