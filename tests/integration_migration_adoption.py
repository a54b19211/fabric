"""Taking over libraries that were migrated before the ledger: observed baselines, postconditions, named mappings."""

import pytest
from integration_migration_baselines import old_module, pg, tables  # noqa: F401 - pg is a fixture
from sqlalchemy import text

from migration import baseline, compat_mappings, fingerprint, ledger, manifests, reference_data, runner
from migration.compat_mappings import CompatMapping
from migration.manifests import MANIFESTS, Manifest

BEFORE = "7337e54"
HISTORY_FILES = MANIFESTS["history"].files
# what the former runners installed: the library a ledger-less deployment has; later files are pending after adoption
COMPAT = manifests.HISTORY_COMPAT_FILES
AFTER = tuple(n for n in HISTORY_FILES if n not in COMPAT)


def old_history(pg, tmp_path, monkeypatch, label="old"):
    """A History library as the former runners left it: no ledger, history_sources seeded by migrate_history."""
    schema, engine = pg(label)
    old_module(BEFORE, tmp_path, "history", monkeypatch, "history_engine", engine).migrate_history()
    old_module(BEFORE, tmp_path, "index_publication", monkeypatch, "history_engine", engine).migrate_index()
    return schema, engine


def events(engine, role="history"):
    with engine.begin() as conn:
        return [tuple(r) for r in conn.execute(text(
            f"SELECT file, event FROM {ledger.EVENTS} WHERE db_role=:r ORDER BY event_id"), {"r": role})]


def synthetic(monkeypatch, tmp_path, files, posts=None, role="history", base=()):
    for name, sql in files.items():
        (tmp_path / name).write_text(sql, encoding="utf-8")
    manifest = Manifest(role, tuple(files), lock_timeout="3s", statement_timeout="30s", base_files=base)
    for module in (runner, baseline, fingerprint):
        monkeypatch.setattr(module, "VERSIONS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(runner, "MANIFESTS", {**MANIFESTS, role: manifest})
    monkeypatch.setattr(baseline, "MANIFESTS", {**MANIFESTS, role: manifest})
    monkeypatch.setattr(baseline, "POSTCONDITIONS", posts or {})
    monkeypatch.setattr(runner, "POSTCONDITIONS", posts or {})
    monkeypatch.setattr(reference_data, "REFERENCE_STEPS", {})
    return manifest


# ---- a real former-runner library ----

def test_a_library_built_by_the_former_runners_is_adopted_with_its_rows_untouched(pg, tmp_path, monkeypatch):
    _, engine = old_history(pg, tmp_path, monkeypatch)
    with engine.begin() as conn:
        seeded = [tuple(r) for r in conn.execute(text("SELECT source_id, catalog FROM history_sources ORDER BY 1"))]
        assert seeded
    _, reference = pg("ref")
    receipt = baseline.adopt("history", engine, reference_engine=reference, actor="test")
    assert receipt["state"] == "adopted" and receipt["event"] == "observed_baseline"
    assert receipt["prefix_length"] == len(COMPAT) and receipt["pending"] == list(AFTER)
    assert events(engine) == [(name, "observed_baseline") for name in COMPAT]
    with engine.begin() as conn:
        assert [tuple(r) for r in conn.execute(text("SELECT source_id, catalog FROM history_sources ORDER BY 1"))] \
            == seeded
        evidence = conn.execute(text(f"SELECT evidence FROM {ledger.CURRENT} WHERE version='V028'")).scalar_one()
    assert evidence["prefix_length"] == len(COMPAT) and evidence["fingerprint_sha256"]
    plan = runner.plan("history", engine)
    assert plan["state"] == "ready" and [p["file"] for p in plan["pending"]] == list(AFTER)
    assert {a["how"] for a in plan["applied"]} == {"observed_baseline"}
    assert all("never" in a["meaning"] or "not say" in a["meaning"] for a in plan["applied"])


def test_after_adoption_a_new_file_is_the_only_thing_the_runner_runs(pg, tmp_path, monkeypatch):
    _, engine = old_history(pg, tmp_path, monkeypatch)
    _, reference = pg("ref")
    baseline.adopt("history", engine, reference_engine=reference, actor="test")
    files = {**{n: (manifests.VERSIONS_DIR / n).read_text(encoding="utf-8") for n in HISTORY_FILES},
             "V990__after.sql": "CREATE TABLE after_adoption(x int);"}
    monkeypatch.setattr(runner, "run_reference_steps", lambda *a, **k: [])
    synthetic(monkeypatch, tmp_path, files)
    receipt = runner.apply("history", engine, actor="test")
    assert [r["file"] for r in receipt["ran"]] == [*AFTER, "V990__after.sql"]
    assert events(engine)[-1] == ("V990__after.sql", "applied")


def test_a_structure_that_matches_no_prefix_gives_a_difference_report_and_writes_nothing(pg, tmp_path, monkeypatch):
    schema, engine = old_history(pg, tmp_path, monkeypatch)
    with engine.begin() as conn:  # a V028 from before piece_key: an earlier content of the same file
        conn.execute(text("DROP INDEX index_publications_artifact_piece_key"))
        conn.execute(text("DROP INDEX index_publications_record_artifact_piece_key"))
        conn.execute(text("ALTER TABLE index_publications DROP COLUMN piece_key"))
    _, reference = pg("ref")
    receipt = baseline.adopt("history", engine, reference_engine=reference, actor="test")
    assert receipt["state"] == "refused" and receipt["reason"] == "fingerprint_matches_no_prefix"
    assert receipt["written"] is False and receipt["diff_sha256"]
    assert "column index_publications.piece_key" in receipt["diff"]["missing"]
    assert isinstance(receipt["nearest_prefix"], int)
    with engine.begin() as conn:
        assert not {ledger.EVENTS, ledger.CURRENT} & tables(conn, schema)


def test_there_is_no_generic_accept_the_difference_switch():
    with pytest.raises(SystemExit):
        runner._parser().parse_args(["--db", "history", "--adopt-baseline", "--accept-diff", "abc"])


def test_a_database_without_the_roles_objects_has_nothing_to_adopt(pg):
    _, engine = pg("nothing")
    _, reference = pg("ref")
    assert baseline.adopt("history", engine, reference_engine=reference)["reason"] == "nothing_to_adopt"


def test_adoption_runs_once_on_an_empty_ledger_only(pg, tmp_path, monkeypatch):
    _, engine = old_history(pg, tmp_path, monkeypatch)
    _, reference = pg("ref")
    assert baseline.adopt("history", engine, reference_engine=reference)["state"] == "adopted"
    before = events(engine)
    _, again = pg("ref2")
    receipt = baseline.adopt("history", engine, reference_engine=again)
    assert receipt["reason"] == "ledger_not_empty" and events(engine) == before


def test_the_reference_library_must_be_empty(pg, tmp_path, monkeypatch):
    _, engine = old_history(pg, tmp_path, monkeypatch)
    _, used = pg("used")
    with used.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE leftover(x int)")
    assert baseline.adopt("history", engine, reference_engine=used)["reason"] == "reference_not_empty"


def test_only_history_and_publication_are_adopted(pg):
    _, engine = pg("app")
    with pytest.raises(Exception, match="object library"):
        baseline.adopt("app", engine)


# ---- rows are not structure ----

TWO = {"V901__table.sql": "CREATE TABLE t(x int);", "V902__rows.sql": "INSERT INTO t VALUES (1);"}


def build(engine, upto):
    with engine.begin() as conn:
        for name in list(TWO)[:upto]:
            conn.exec_driver_sql(TWO[name])


def test_a_data_file_with_no_trace_and_no_postcondition_cannot_be_told_and_is_refused(pg, monkeypatch, tmp_path):
    schema, engine = pg("undecidable")
    synthetic(monkeypatch, tmp_path, TWO)
    build(engine, 2)
    _, reference = pg("ref")
    receipt = baseline.adopt("history", engine, reference_engine=reference)
    assert receipt["state"] == "refused" and receipt["reason"] == "data_file_without_postcondition"
    assert receipt["file"] == "V902__rows.sql"
    with engine.begin() as conn:
        assert ledger.EVENTS not in tables(conn, schema)


def test_the_longest_prefix_is_not_taken_when_the_data_step_is_not_done(pg, monkeypatch, tmp_path):
    _, engine = pg("notdone")
    synthetic(monkeypatch, tmp_path, TWO, posts={"V902__rows.sql": ("SELECT count(*) = 1 FROM t",)})
    build(engine, 1)  # the table is there, the row is not
    _, reference = pg("ref")
    receipt = baseline.adopt("history", engine, reference_engine=reference)
    assert receipt["state"] == "adopted" and receipt["prefix_length"] == 1
    assert receipt["adopted"] == ["V901__table.sql"] and receipt["pending"] == ["V902__rows.sql"]
    runner.apply("history", engine, actor="test")  # the data step then runs, once
    assert events(engine) == [("V901__table.sql", "observed_baseline"), ("V902__rows.sql", "applied")]
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM t")).scalar_one() == 1


def test_the_longest_prefix_is_taken_when_the_postcondition_holds(pg, monkeypatch, tmp_path):
    _, engine = pg("done")
    synthetic(monkeypatch, tmp_path, TWO, posts={"V902__rows.sql": ("SELECT count(*) = 1 FROM t",)})
    build(engine, 2)
    _, reference = pg("ref")
    receipt = baseline.adopt("history", engine, reference_engine=reference)
    assert receipt["prefix_length"] == 2 and receipt["pending"] == []


def test_a_data_file_with_a_structural_trace_still_needs_its_postcondition_to_hold(pg, tmp_path, monkeypatch):
    schema, engine = old_history(pg, tmp_path, monkeypatch)
    monkeypatch.setattr(baseline, "POSTCONDITIONS", {"V028__index_worker.sql": ("SELECT false",)})
    _, reference = pg("ref")
    receipt = baseline.adopt("history", engine, reference_engine=reference)
    assert receipt["state"] == "refused" and receipt["reason"] == "postcondition_failed"
    assert receipt["file"] == "V028__index_worker.sql" and "SELECT false" in receipt["detail"]
    with engine.begin() as conn:
        assert ledger.EVENTS not in tables(conn, schema)


def test_a_data_file_without_any_declaration_stops_the_adoption(pg, monkeypatch, tmp_path):
    _, engine = pg("undeclared")
    synthetic(monkeypatch, tmp_path, {"V901__rows.sql": "CREATE TABLE t(x int);\nINSERT INTO t VALUES (1);"})
    build_sql = "CREATE TABLE t(x int);"
    with engine.begin() as conn:
        conn.exec_driver_sql(build_sql)
    _, reference = pg("ref")
    assert baseline.adopt("history", engine, reference_engine=reference)["reason"] == "data_file_without_postcondition"


# ---- named mappings ----

def odd_library(pg, tmp_path, monkeypatch):
    schema, engine = old_history(pg, tmp_path, monkeypatch, "odd")
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX index_publications_artifact_piece_key"))
        conn.execute(text("DROP INDEX index_publications_record_artifact_piece_key"))
        conn.execute(text("ALTER TABLE index_publications DROP COLUMN piece_key"))
    return schema, engine


def mapping(sha, treatment="equivalent_to_prefix", target=len(HISTORY_FILES) - 1, role="history"):
    return CompatMapping(name="piece-key-store", db_role=role, observed_fingerprint_sha256=sha, meaning="a store "
                         "from before piece_key; reviewed", treatment=treatment, target=target, reviewed_by="test")


def sha_of(engine, reference_pg):
    refused = baseline.adopt("history", engine, reference_engine=reference_pg("probe")[1])
    assert refused["state"] == "refused"
    return refused["diff_sha256"]


def test_a_named_mapping_that_matches_the_exact_observation_is_accepted_as_compat_mapped(pg, tmp_path, monkeypatch):
    schema, engine = odd_library(pg, tmp_path, monkeypatch)
    sha = sha_of(engine, pg)
    # the mapping says: this structure counts as the nine files before V028 (the index store of that time)
    monkeypatch.setattr(compat_mappings, "COMPAT_MAPPINGS", (mapping(sha, target=9),))
    receipt = baseline.adopt("history", engine, reference_engine=pg("ref")[1])
    assert receipt["state"] == "adopted" and receipt["event"] == "compat_mapped"
    assert receipt["evidence"]["mapping"] == "piece-key-store" and receipt["prefix_length"] == 9
    assert receipt["pending"][0] == "V028__index_worker.sql"
    assert events(engine) == [(name, "compat_mapped") for name in HISTORY_FILES[:9]]


def test_a_mapping_for_another_fingerprint_or_role_changes_nothing(pg, tmp_path, monkeypatch):
    schema, engine = odd_library(pg, tmp_path, monkeypatch)
    sha = sha_of(engine, pg)
    monkeypatch.setattr(compat_mappings, "COMPAT_MAPPINGS", (mapping("0" * 64), mapping(sha, role="publication")))
    receipt = baseline.adopt("history", engine, reference_engine=pg("ref")[1])
    assert receipt["reason"] == "fingerprint_matches_no_prefix"


def test_a_mapping_that_requires_a_fix_migration_names_it_and_writes_nothing(pg, tmp_path, monkeypatch):
    schema, engine = odd_library(pg, tmp_path, monkeypatch)
    sha = sha_of(engine, pg)
    monkeypatch.setattr(compat_mappings, "COMPAT_MAPPINGS",
                        (mapping(sha, treatment="requires_fix_migration", target="V999"),))
    receipt = baseline.adopt("history", engine, reference_engine=pg("ref")[1])
    assert receipt["reason"] == "requires_fix_migration" and receipt["fix_migration"] == "V999"
    assert receipt["written"] is False
    with engine.begin() as conn:
        assert ledger.EVENTS not in tables(conn, schema)


def test_a_revoked_adoption_can_be_adopted_again_by_a_new_observation(pg, tmp_path, monkeypatch):
    _, engine = old_history(pg, tmp_path, monkeypatch)
    baseline.adopt("history", engine, reference_engine=pg("ref")[1], actor="test")
    for name in COMPAT:
        runner.revoke_baseline("history", engine, name.split("__")[0], reason="wrong library", actor="test")
    assert runner.plan("history", engine)["state"] == "refused"
    receipt = baseline.adopt("history", engine, reference_engine=pg("ref2")[1], actor="test")
    assert receipt["state"] == "adopted"
    assert [p["file"] for p in runner.plan("history", engine)["pending"]] == list(AFTER)
    with engine.begin() as conn:
        kinds = [e for (e,) in conn.execute(text(
            f"SELECT event FROM {ledger.EVENTS} WHERE version='V017' ORDER BY event_id"))]
    assert kinds == ["observed_baseline", "baseline_revoked", "observed_baseline"]


def test_the_publication_role_is_adopted_on_top_of_the_object_library_base(pg, tmp_path, monkeypatch):
    schema, engine = pg("pubold")
    with engine.begin() as conn:
        for name in manifests.PUBLICATION_BASE_FILES:
            conn.exec_driver_sql((manifests.VERSIONS_DIR / name).read_text(encoding="utf-8"))
    old_module(BEFORE, tmp_path, "publication", monkeypatch, "publication_engine", engine).migrate_publication()
    receipt = baseline.adopt("publication", engine, reference_engine=pg("ref")[1], actor="test")
    assert receipt["state"] == "adopted" and receipt["adopted"] == list(MANIFESTS["publication"].files)
    assert runner.plan("publication", engine)["state"] == "up_to_date"
