"""The common migration runner on real PostgreSQL (throwaway schemas): ledger, plan, refusals, lock, corrections."""

import json
import re
import threading

import pytest
from integration_migration_baselines import pg, tables  # noqa: F401 - pg is a fixture
from sqlalchemy import text

from migration import ledger, manifests, reference_data, runner
from migration.app_ledger import MigrationStateError
from migration.manifests import MANIFESTS, Manifest

HISTORY_FILES = MANIFESTS["history"].files


def event_rows(engine, role="history"):
    with engine.begin() as conn:
        return [tuple(r) for r in conn.execute(text(
            f"SELECT file, event FROM {ledger.EVENTS} WHERE db_role=:r ORDER BY event_id"), {"r": role})]


def synthetic(monkeypatch, tmp_path, files, role="history", **options):
    """A role whose manifest is the given {name: sql} files in tmp_path."""
    for name, sql in files.items():
        (tmp_path / name).write_text(sql, encoding="utf-8")
    manifest = Manifest(role, tuple(files), lock_timeout="3s", statement_timeout="30s", **options)
    monkeypatch.setattr(runner, "MANIFESTS", {**MANIFESTS, role: manifest})
    monkeypatch.setattr(runner, "VERSIONS_DIR", tmp_path)
    monkeypatch.setattr(runner, "POSTCONDITIONS", {})
    monkeypatch.setattr(runner, "TRANSACTION_CONTROL_ALLOWED", frozenset())
    monkeypatch.setattr(reference_data, "REFERENCE_STEPS", {})
    return manifest


# ---- history role from an empty database ----

def test_a_fresh_history_database_gets_every_file_and_one_applied_event_each(pg):
    schema, engine = pg("fresh")
    receipt = runner.apply("history", engine, actor="test")
    assert receipt["state"] == "applied" and [r["file"] for r in receipt["ran"]] == list(HISTORY_FILES)
    assert event_rows(engine) == [(name, "applied") for name in HISTORY_FILES]
    with engine.begin() as conn:
        assert {"history_sources", "index_targets", "legacy_aliases", "derived_extraction_checks"} <= tables(conn, schema)
        rows = conn.execute(text(f"SELECT event, sha256, actor FROM {ledger.CURRENT} WHERE db_role='history'")).all()
        assert len(rows) == len(HISTORY_FILES) and {r[0] for r in rows} == {"applied"}
        assert all(re.fullmatch(r"[0-9a-f]{64}", r[1]) and r[2] == "test" for r in rows)
        assert conn.execute(text("SELECT count(*) FROM history_sources")).scalar_one() > 0  # the reference step ran
        logged = conn.execute(text(f"SELECT step FROM {ledger.REFERENCE_LOG} WHERE db_role='history'")).scalars().all()
        assert logged == ["seed_history_sources"]


def test_a_second_run_finds_nothing_to_do_and_writes_nothing(pg):
    _, engine = pg("again")
    runner.apply("history", engine, actor="test")
    before = event_rows(engine)
    receipt = runner.apply("history", engine, actor="test")
    assert receipt["state"] == "up_to_date" and receipt["ran"] == []
    assert event_rows(engine) == before


def test_the_ledger_cannot_be_updated_deleted_or_truncated(pg):
    _, engine = pg("append")
    runner.apply("history", engine, actor="test")
    for statement in (f"UPDATE {ledger.EVENTS} SET actor='x'", f"DELETE FROM {ledger.EVENTS}",
                      f"TRUNCATE {ledger.EVENTS}", f"UPDATE {ledger.REFERENCE_LOG} SET actor='x'"):
        with pytest.raises(Exception, match="append-only"), engine.begin() as conn:
            conn.exec_driver_sql(statement)
    assert len(event_rows(engine)) == len(HISTORY_FILES)


# ---- plan ----

def test_plan_on_an_empty_database_lists_everything_and_creates_no_table(pg):
    schema, engine = pg("plan")
    receipt = runner.plan("history", engine)
    assert receipt["ledger"] == "absent" and receipt["state"] == "ready"
    assert [p["file"] for p in receipt["pending"]] == list(HISTORY_FILES)
    assert receipt["runner"] == "fabric.migration.runner.v1" and receipt["applied"] == []
    with engine.begin() as conn:
        assert tables(conn, schema) == set()  # N14: no ledger table, nothing else


def test_plan_after_apply_is_up_to_date_and_still_writes_nothing(pg):
    _, engine = pg("planafter")
    runner.apply("history", engine, actor="test")
    before = event_rows(engine)
    receipt = runner.plan("history", engine)
    assert receipt["state"] == "up_to_date" and receipt["pending"] == [] and receipt["ledger"] == "present"
    assert {a["how"] for a in receipt["applied"]} == {"applied"}
    assert event_rows(engine) == before


# ---- refusals ----

def test_objects_without_a_ledger_are_refused_with_the_hint_to_adopt(pg):
    schema, engine = pg("legacy")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE history_sources(source_id text)")
    receipt = runner.plan("history", engine)
    assert receipt["state"] == "refused" and "--adopt-baseline" in receipt["reason"]
    with pytest.raises(MigrationStateError, match="adopt-baseline"):
        runner.apply("history", engine, actor="test")
    with engine.begin() as conn:
        assert tables(conn, schema) == {"history_sources"}  # nothing created, no ledger


def test_a_recorded_file_with_foreign_content_is_refused_and_nothing_is_written(pg):
    _, engine = pg("changed")
    runner.apply("history", engine, actor="test")
    with engine.begin() as conn:  # a later event for V017 claiming content this file never had
        conn.execute(text(
            f"INSERT INTO {ledger.EVENTS}(db_role,version,file,event,checksum_md5,sha256,actor) "
            "VALUES ('history','V017','V017__history_records.sql','applied',:m,:s,'tamper')"),
            {"m": "0" * 32, "s": "0" * 64})
    before = event_rows(engine)
    receipt = runner.plan("history", engine)
    assert receipt["state"] == "refused" and "no content this file ever had" in receipt["reason"]
    with pytest.raises(MigrationStateError, match="no content this file ever had"):
        runner.apply("history", engine, actor="test")
    assert event_rows(engine) == before


def test_a_recorded_file_the_manifest_no_longer_lists_is_refused(pg, monkeypatch):
    _, engine = pg("dropped")
    runner.apply("history", engine, actor="test")
    shortened = Manifest("history", HISTORY_FILES[1:], lock_timeout="3s", statement_timeout="60s")
    monkeypatch.setattr(runner, "MANIFESTS", {**MANIFESTS, "history": shortened})
    assert "does not list" in runner.plan("history", engine)["reason"]


def test_a_gap_in_the_applied_files_is_refused(pg, monkeypatch, tmp_path):
    _, engine = pg("gap")
    synthetic(monkeypatch, tmp_path, {"V901__a.sql": "CREATE TABLE a(x int);", "V902__b.sql": "CREATE TABLE b(x int);",
                                      "V903__c.sql": "CREATE TABLE c(x int);"})
    with engine.begin() as conn:
        ledger.ensure(conn)
        ledger.append(conn, "history", "V903__c.sql", "applied", "CREATE TABLE c(x int);", actor="test")
    assert "before it is not" in runner.plan("history", engine)["reason"]


def test_a_file_with_a_function_body_runs_whole_where_the_semicolon_split_breaks_it(pg, monkeypatch, tmp_path):
    _, engine = pg("function")
    sql = ("CREATE TABLE t(a int);\nCREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$\n"
           "BEGIN\n  RETURN 1;\nEND;\n$$;\n")
    synthetic(monkeypatch, tmp_path, {"V901__function.sql": sql})
    with pytest.raises(Exception):  # the algorithm of the former history/index runners, kept here as the contrast
        with engine.begin() as conn:
            for statement in sql.split(";"):
                statement = "\n".join(s for s in statement.splitlines() if not s.lstrip().startswith("--")).strip()
                if statement:
                    conn.execute(text(statement))
    runner.apply("history", engine, actor="test")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT f()")).scalar_one() == 1


def test_concurrently_is_refused_at_its_file_and_earlier_files_stay_recorded(pg, monkeypatch, tmp_path):
    _, engine = pg("concurrently")
    synthetic(monkeypatch, tmp_path, {
        "V901__a.sql": "CREATE TABLE a(x int);", "V902__b.sql": "CREATE INDEX CONCURRENTLY ia ON a(x);",
        "V903__c.sql": "CREATE TABLE c(x int);"})
    receipt = runner.plan("history", engine)
    assert receipt["state"] == "refused" and "CONCURRENTLY" in receipt["reason"]
    with pytest.raises(MigrationStateError, match="V902.*CONCURRENTLY"):
        runner.apply("history", engine, actor="test")
    assert event_rows(engine) == [("V901__a.sql", "applied")]  # recorded; the refused file and the later one not run


def test_a_failing_file_rolls_back_whole_and_the_next_run_continues_from_it(pg, monkeypatch, tmp_path):
    schema, engine = pg("failing")
    synthetic(monkeypatch, tmp_path, {
        "V901__a.sql": "CREATE TABLE a(x int);",
        "V902__b.sql": "CREATE TABLE b(x int);\nINSERT INTO a VALUES ('not a number');"})
    with pytest.raises(Exception):
        runner.apply("history", engine, actor="test")
    with engine.begin() as conn:
        assert "b" not in tables(conn, schema) and "a" in tables(conn, schema)
    assert event_rows(engine) == [("V901__a.sql", "applied")]
    (tmp_path / "V902__b.sql").write_text("CREATE TABLE b(x int);", encoding="utf-8")
    runner.apply("history", engine, actor="test")
    assert event_rows(engine) == [("V901__a.sql", "applied"), ("V902__b.sql", "applied")]


def test_a_postcondition_that_fails_after_a_file_rolls_it_back(pg, monkeypatch, tmp_path):
    schema, engine = pg("post")
    synthetic(monkeypatch, tmp_path, {"V901__a.sql": "CREATE TABLE a(x int);\nINSERT INTO a VALUES (1);"})
    monkeypatch.setattr(runner, "POSTCONDITIONS", {"V901__a.sql": ("SELECT count(*) = 2 FROM a",)})
    with pytest.raises(MigrationStateError, match="postcondition"):
        runner.apply("history", engine, actor="test")
    with engine.begin() as conn:
        assert tables(conn, schema) <= {ledger.EVENTS, ledger.REFERENCE_LOG, ledger.CURRENT}  # the file's table is gone
    assert event_rows(engine) == []


# ---- the lock over a whole run ----

def test_a_run_started_while_another_holds_the_lock_exits_without_writing(pg):
    schema, engine = pg("locked")
    with runner.migration_lock(engine):
        with pytest.raises(runner.MigrationBusy, match="another migration"):
            runner.apply("history", engine, actor="test")
        with pytest.raises(runner.MigrationBusy):  # another role of the same physical database
            runner.apply("app", engine, actor="test")
    with engine.begin() as conn:
        assert tables(conn, schema) == set()
    runner.apply("history", engine, actor="test")  # the lock was released


def test_the_lock_is_released_when_the_run_fails(pg, monkeypatch, tmp_path):
    _, engine = pg("lockfail")
    synthetic(monkeypatch, tmp_path, {"V901__a.sql": "SELECT 1/0;"})
    with pytest.raises(Exception):
        runner.apply("history", engine, actor="test")
    with runner.migration_lock(engine):
        pass


def test_two_runners_started_together_on_an_empty_database_do_not_both_run(pg, monkeypatch, tmp_path):
    _, engine = pg("race")
    synthetic(monkeypatch, tmp_path, {"V901__slow.sql": "SELECT pg_sleep(1.5); CREATE TABLE a(x int);"})
    outcomes = []

    def go():
        try:
            runner.apply("history", engine, actor="test")
            outcomes.append("ran")
        except runner.MigrationBusy:
            outcomes.append("busy")

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["busy", "ran"]
    assert event_rows(engine) == [("V901__slow.sql", "applied")]


# ---- corrections are events ----

def test_a_revoked_baseline_shows_as_absent_and_refuses_until_it_is_resolved(pg):
    _, engine = pg("revoke")
    runner.apply("history", engine, actor="test")
    with engine.begin() as conn:  # an observed baseline event for the last file, as --adopt-baseline writes it
        ledger.append(conn, "history", HISTORY_FILES[-1], "observed_baseline",
                      (manifests.VERSIONS_DIR / HISTORY_FILES[-1]).read_text(encoding="utf-8"), actor="test",
                      evidence={"prefix": len(HISTORY_FILES)})
    version = HISTORY_FILES[-1].split("__")[0]
    receipt = runner.revoke_baseline("history", engine, version, reason="wrong database", actor="test")
    assert receipt["state"] == "revoked"
    with engine.begin() as conn:
        current = conn.execute(text(f"SELECT event, evidence FROM {ledger.CURRENT} WHERE version=:v"),
                               {"v": version}).one()
        assert current[0] == "baseline_revoked" and current[1] == {"reason": "wrong database"}
        history = [e for (e,) in conn.execute(text(
            f"SELECT event FROM {ledger.EVENTS} WHERE version=:v ORDER BY event_id"), {"v": version})]
        assert history == ["applied", "observed_baseline", "baseline_revoked"]  # nothing was rewritten
    refused = runner.plan("history", engine)
    assert refused["state"] == "refused" and "revoked" in refused["reason"]


def test_only_an_observed_or_mapped_baseline_can_be_revoked_not_a_real_execution(pg):
    _, engine = pg("norevoke")
    runner.apply("history", engine, actor="test")
    with pytest.raises(MigrationStateError, match="no observed_baseline"):
        runner.revoke_baseline("history", engine, "V017", reason="x", actor="test")


# ---- the object library through the same runner ----

def test_the_object_library_keeps_schema_migrations_and_gains_the_sha256_events(pg):
    _, engine = pg("app")
    runner.apply("app", engine, actor="test")
    with engine.begin() as conn:
        versions = [v for (v,) in conn.execute(text("SELECT version FROM schema_migrations ORDER BY version"))]
        assert versions == [f"V{n:03}" for n in range(1, 36)]
        events = conn.execute(text(f"SELECT file, sha256 FROM {ledger.EVENTS} WHERE db_role='app' ORDER BY file")).all()
    assert [f for f, _ in events] == list(manifests.APP_COMPAT_FILES)
    receipt = runner.plan("app", engine)
    assert receipt["state"] == "up_to_date" and receipt["ledger"] == "present"
    assert json.dumps(receipt)  # plain data


def test_the_object_library_plan_lists_the_35_compatibility_files_and_no_other(pg):
    _, engine = pg("appplan")
    receipt = runner.plan("app", engine)
    assert receipt["ledger"] == "absent" and [p["file"] for p in receipt["pending"]] == list(manifests.APP_COMPAT_FILES)
