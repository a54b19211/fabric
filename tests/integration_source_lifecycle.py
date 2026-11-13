"""History source lifecycle (V036): the retirement decision, the deployment's effective time, rollback, old data untouched."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from integration_migration_adoption import old_history  # noqa: F401 - builds a library as the former runners did
from integration_migration_baselines import pg  # noqa: F401 - fixture
from sqlalchemy import text

from fetch.retired_sources import RETIRED
from ops import source_lifecycle
from store.history_models import HistoryError
from store.migrations import baseline, runner


def events(engine):
    with engine.begin() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT event_id, source_id, event, at, at_precision, recorded_at, decision_ref, actor "
            "FROM history_source_lifecycle ORDER BY event_id")).mappings()]


@pytest.fixture
def library(pg):
    _, engine = pg("lifecycle")
    runner.apply("history", engine, actor="test")
    return engine


def test_the_migration_records_the_decision_as_a_day_not_as_an_effective_time(library):
    (decided,) = events(library)
    assert (decided["source_id"], decided["event"], decided["at_precision"]) == (
        "legacy_documents", "retirement_decided", "day")
    assert decided["at"] == datetime(2026, 11, 6, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert decided["decision_ref"] == "雷达全链路设计-v3.1.md#6.3" and decided["actor"] == "migration:V036"
    assert decided["recorded_at"] > decided["at"]  # written when the migration ran, not back-dated to the decision


def test_running_the_file_again_does_not_record_the_decision_twice(library):
    from store.migrations.manifests import VERSIONS_DIR

    with library.begin() as conn:
        conn.exec_driver_sql((VERSIONS_DIR / "V036__history_source_lifecycle.sql").read_text(encoding="utf-8"))
    assert [e["event"] for e in events(library)] == ["retirement_decided"]


def test_the_effective_time_is_the_database_clock_when_it_is_written(library):
    before = datetime.now(timezone.utc)
    with library.begin() as conn:
        written = source_lifecycle.record(conn, "legacy_documents", "effective", actor="deploy:test")
    after = datetime.now(timezone.utc)
    decided, effective = events(library)
    assert before - timedelta(seconds=5) <= effective["at"] <= after + timedelta(seconds=5)
    assert effective["at"] > decided["at"] and effective["at"] != decided["at"]  # never the decision date
    assert effective["at_precision"] == "instant" and effective["decision_ref"] == decided["decision_ref"]
    assert effective["actor"] == "deploy:test" and written["event"] == "retirement_effective"


def test_a_retirement_takes_effect_once_and_can_be_withdrawn_and_taken_again(library):
    def write(kind):
        with library.begin() as conn:
            return source_lifecycle.record(conn, "legacy_documents", kind, actor="deploy:test")

    with pytest.raises(HistoryError, match="not in effect"):
        write("withdrawn")
    write("effective")
    with pytest.raises(HistoryError, match="already in effect"):
        write("effective")
    write("withdrawn")
    write("effective")
    assert [e["event"] for e in events(library)] == [
        "retirement_decided", "retirement_effective", "retirement_withdrawn", "retirement_effective"]


def test_only_a_source_the_code_retires_and_the_log_decided_can_take_effect(library):
    with library.begin() as conn:
        with pytest.raises(HistoryError, match="not retired by the code"):
            source_lifecycle.record(conn, "pubmed", "effective", actor="x")
    assert len(events(library)) == 1


def test_the_log_cannot_be_rewritten_or_emptied(library):
    for statement in ("UPDATE history_source_lifecycle SET actor='x'", "DELETE FROM history_source_lifecycle",
                      "TRUNCATE history_source_lifecycle"):
        with pytest.raises(Exception, match="append-only"), library.begin() as conn:
            conn.exec_driver_sql(statement)
    assert len(events(library)) == 1


def snapshot(engine):
    with engine.begin() as conn:
        return {table: [dict(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).mappings()]
                for table in ("history_sources", "legacy_aliases", "history_runs", "history_versions")}


def test_old_legacy_rows_and_their_audit_are_untouched_by_the_migration(pg, tmp_path, monkeypatch):
    _, engine = old_history(pg, tmp_path, monkeypatch, "legacyrows")
    with engine.begin() as conn:  # the row the earlier seed gave the bridge (the catalog no longer lists it)
        conn.execute(text("INSERT INTO history_sources(source_id, catalog, published_seq) "
                          "VALUES ('legacy_documents', CAST(:c AS jsonb), 7) ON CONFLICT (source_id) DO UPDATE "
                          "SET published_seq=7"), {"c": json.dumps(RETIRED["legacy_documents"].catalog_entry())})
        conn.execute(text(
            "INSERT INTO legacy_aliases(legacy_version_id, legacy_record_id, is_demo, status, reason, converter) "
            "VALUES (repeat('a', 64), 'legacy:one', false, 'pending', 'detail_url_not_proven', 'test')"))
    before = snapshot(engine)
    assert baseline.adopt("history", engine, reference_engine=pg("ref")[1], actor="test")["prefix_length"] == 14
    runner.apply("history", engine, actor="test")
    assert snapshot(engine) == before
    assert [e["event"] for e in events(engine)] == ["retirement_decided"]
