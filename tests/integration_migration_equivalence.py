"""A fresh install through the common runner has the structure the former executors (as of 7337e54) produced."""

from integration_migration_baselines import diff, fingerprint, old_module, pg  # noqa: F401 - pg is a fixture
from sqlalchemy import text

from migration import manifests, runner

BEFORE = "7337e54"


# added after 7337e54: the runner's own ledger, the lifecycle log of V036, the measurement tables of V037/V038
NEWER = ("fabric_", "history_source_lifecycle", "derived_measure")


def without_ledger(fp):
    """The structure, minus what the runner and the files after V035 add to a library of 7337e54."""
    return {kind: [row for row in rows if not any(n in " ".join(str(v) for v in row) for n in NEWER)]
            for kind, rows in fp.items()}


def test_the_history_role_equals_history_then_index_of_the_former_runners(pg, tmp_path, monkeypatch):
    old_schema, old_engine = pg("histold")
    old_module(BEFORE, tmp_path, "history", monkeypatch, "history_engine", old_engine).migrate_history()
    old_module(BEFORE, tmp_path, "index_publication", monkeypatch, "history_engine", old_engine).migrate_index()
    new_schema, new_engine = pg("histnew")
    runner.apply("history", new_engine, actor="test")
    assert diff(without_ledger(fingerprint(new_engine, new_schema)), fingerprint(old_engine, old_schema)) == {}


def test_the_history_files_in_version_order_equal_the_two_stage_order(pg, tmp_path, monkeypatch):
    """The former order was the nine history files, then the five index files; version order interleaves them."""
    two_stage = tuple(sorted(manifests.HISTORY_COMPAT_FILES, key=lambda n: (n[:4] not in {
        "V017", "V018", "V021", "V022", "V023", "V025", "V029", "V030", "V032"}, n)))
    assert two_stage != manifests.HISTORY_COMPAT_FILES
    schema, engine = pg("stages")
    with engine.begin() as conn:
        for name in two_stage:
            conn.exec_driver_sql((manifests.VERSIONS_DIR / name).read_text(encoding="utf-8"))
    new_schema, new_engine = pg("versionorder")
    with new_engine.begin() as conn:
        for name in manifests.HISTORY_COMPAT_FILES:
            conn.exec_driver_sql((manifests.VERSIONS_DIR / name).read_text(encoding="utf-8"))
    assert diff(fingerprint(new_engine, new_schema), fingerprint(engine, schema)) == {}


def test_the_publication_role_equals_the_former_runner(pg, tmp_path, monkeypatch):
    def base(engine):
        with engine.begin() as conn:
            for name in manifests.PUBLICATION_BASE_FILES:
                conn.exec_driver_sql((manifests.VERSIONS_DIR / name).read_text(encoding="utf-8"))

    old_schema, old_engine = pg("pubold")
    base(old_engine)
    old_module(BEFORE, tmp_path, "publication", monkeypatch, "publication_engine", old_engine).migrate_publication()
    new_schema, new_engine = pg("pubnew")
    base(new_engine)
    runner.apply("publication", new_engine, actor="test")
    assert diff(without_ledger(fingerprint(new_engine, new_schema)), fingerprint(old_engine, old_schema)) == {}
    with new_engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM fabric_schema_ledger_events WHERE db_role='publication'")
                            ).scalar_one() == 2


def test_the_object_library_equals_the_former_runner(pg, tmp_path, monkeypatch):
    old_schema, old_engine = pg("appold")
    old_module(BEFORE, tmp_path, "migrate", monkeypatch, "get_engine", old_engine).run_migrations()
    new_schema, new_engine = pg("appnew")
    runner.apply("app", new_engine, actor="test")
    assert diff(without_ledger(fingerprint(new_engine, new_schema)), fingerprint(old_engine, old_schema)) == {}
