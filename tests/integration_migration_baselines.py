"""The object-library migration runner on real baselines (real PostgreSQL, throwaway schemas).

Two databases are first migrated by the code of the two lines this branch merged, taken from the repository history
(git archive), then filled with rows, then upgraded by today's runner:
- the rebuild line (c9ed485): its runner, then its migration.publication (its runner cannot run V019 - it splits
  SQL on semicolons inside function bodies - and the line installs V019/V026 with migration.publication);
- the team line (d316aad, feat/server_fix): its runner, which recorded its own V020-V023.
Both must keep every original schema_migrations row and every data row, and end with the schema of a fresh install.
A database with a record no commit ever had, or with a superseded one, is refused with nothing written.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from store.migrations import app_ledger, manifests, runner

REPO = Path(__file__).resolve().parents[1]
REBUILD_LINE, TEAM_LINE = "c9ed485", "d316aad"
TABLES = ("documents", "entity_dict", "extracted_entities", "publication_outbox", "doc_authority_resolution")


@pytest.fixture
def pg():
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL URL required")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.port == 18436 and parsed.database == "fabric_history"
    admin = create_engine(url)
    created = []

    def schema(label):
        name = "mig_" + label + "_" + uuid4().hex[:12]
        with admin.begin() as conn:
            conn.execute(text("CREATE SCHEMA " + name))
        created.append(name)
        return name, create_engine(url, connect_args={"options": "-csearch_path=" + name + ",public"})

    yield schema
    if os.environ.get("DATAFABRIC_MIGRATION_KEEP_SCHEMAS") != "1":
        with admin.begin() as conn:
            for name in created:
                conn.execute(text("DROP SCHEMA " + name + " CASCADE"))
    else:
        print("kept schemas:", " ".join(created))
    admin.dispose()


# the top-level packages before r1 moved them into fetch/ clean/ store/ graph/ run/
OLD_PACKAGES = {"biz_rules", "collection", "config", "contracts", "core_engine", "db", "disambiguation", "flows",
                "migration", "processing"}


def old_module(commit, tmp_path, name, monkeypatch, attribute, engine):
    """A module of migration/ exactly as `commit` had it (with its own versions/ directory), bound to `engine`. It is
    imported from that commit's whole tree, put first on sys.path: the modules it imports had other names then."""
    root = tmp_path / commit
    if not root.exists():
        archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", commit],
                                 capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(root)
    for loaded in [m for m in sys.modules if m.split(".")[0] in OLD_PACKAGES]:
        monkeypatch.delitem(sys.modules, loaded)
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location(f"migration_{name}_{commit}", root / "migration" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, attribute, lambda: engine)
    return module


def run_today(monkeypatch, engine):
    runner.apply("app", engine)


def seed(engine, *, authority):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO documents(uuid,title,raw_content,source_site) VALUES "
                          "('mig-doc-1','SYNTHETIC one','SYNTHETIC body one','clinicaltrials'),"
                          "('mig-doc-2','SYNTHETIC two','SYNTHETIC body two','pubmed')"))
        conn.execute(text("""INSERT INTO entity_dict(entity_type,canonical_data)
                             VALUES ('Company','{"name":"SYNTHETIC Co"}')"""))
        conn.execute(text("INSERT INTO extracted_entities(source_doc_uuid,entity_type,entity_name,source_text) "
                          "VALUES ('mig-doc-1','Company','SYNTHETIC Co','SYNTHETIC Co')"))
        if authority:
            conn.execute(text("INSERT INTO doc_authority_resolution(uuid,source_site,resolution_key,found) "
                              "VALUES ('mig-doc-1','clinicaltrials','SYNTHETIC drug',true)"))


def tables(conn, schema):
    return {name for (name,) in conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=:s"), {"s": schema})}


def snapshot(engine, schema):
    """schema_migrations rows and the data rows, each table with the columns it has now."""
    with engine.begin() as conn:
        present = tables(conn, schema)
        data = {}
        for table in TABLES:
            if table in present:
                columns = [c for (c,) in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=:s AND table_name=:t "
                    "ORDER BY ordinal_position"), {"s": schema, "t": table})]
                data[table] = (columns, [list(r) for r in conn.execute(text(
                    f"SELECT {','.join(columns)} FROM {table} ORDER BY id"))])
        records = [list(r) for r in conn.execute(text(
            "SELECT version,description,checksum,applied_at FROM schema_migrations ORDER BY version"))]
    return records, data


def same_rows(engine, before):
    """The rows of `before`, read again with the columns they had then."""
    records, data = before
    with engine.begin() as conn:
        now = {tuple(r) for r in conn.execute(text(
            "SELECT version,description,checksum,applied_at FROM schema_migrations"))}
        assert {tuple(r) for r in records} <= now  # every original record kept as it was
        for table, (columns, rows) in data.items():
            again = [list(r) for r in conn.execute(text(f"SELECT {','.join(columns)} FROM {table} ORDER BY id"))]
            assert again == rows, table


def fingerprint(engine, schema):
    """Every object of the schema, independent of column order and of the schema's name."""
    def clean(value):
        return None if value is None else str(value).replace(schema + ".", "")

    queries = {
        "columns": "SELECT table_name,column_name,data_type,udt_name,is_nullable,column_default,"
                   "character_maximum_length FROM information_schema.columns WHERE table_schema=:s",
        "constraints": "SELECT c.conrelid::regclass::text,c.conname,pg_get_constraintdef(c.oid) FROM pg_constraint c "
                       "JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=:s",
        "indexes": "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=:s",
        "triggers": "SELECT t.tgname,pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:s AND NOT t.tgisinternal",
        "functions": "SELECT p.proname,pg_get_function_identity_arguments(p.oid),p.prosrc FROM pg_proc p "
                     "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:s",
        "rules": "SELECT tablename,rulename,definition FROM pg_rules WHERE schemaname=:s",
        "views": "SELECT viewname,definition FROM pg_views WHERE schemaname=:s",
        "sequences": "SELECT sequence_name,data_type FROM information_schema.sequences WHERE sequence_schema=:s",
    }
    with engine.begin() as conn:
        return {kind: sorted(tuple(clean(v) for v in row) for row in conn.execute(text(sql), {"s": schema}))
                for kind, sql in queries.items()}


def diff(left, right):
    return {kind: (sorted(set(left[kind]) - set(right[kind])), sorted(set(right[kind]) - set(left[kind])))
            for kind in left if set(left[kind]) != set(right[kind])}


@pytest.fixture
def target(pg, monkeypatch):
    schema, engine = pg("fresh")
    run_today(monkeypatch, engine)
    return fingerprint(engine, schema)


def test_a_rebuild_line_database_upgrades_in_place(pg, target, tmp_path, monkeypatch):
    schema, engine = pg("rebuild")
    old = old_module(REBUILD_LINE, tmp_path, "migrate", monkeypatch, "get_engine", engine)
    with pytest.raises(Exception):  # the line's own runner stops at V019 (dollar-quoted bodies split on ";")
        old.run_migrations()
    old_module(REBUILD_LINE, tmp_path, "publication", monkeypatch, "publication_engine", engine).migrate_publication()
    seed(engine, authority=False)
    before = snapshot(engine, schema)
    assert [r[0] for r in before[0]] == [f"V{n:03}" for n in range(1, 19)]
    assert before[1]["publication_outbox"][1]  # the V019 trigger recorded the seeded documents

    run_today(monkeypatch, engine)
    run_today(monkeypatch, engine)  # a second run finds nothing to do
    same_rows(engine, before)
    with engine.begin() as conn:
        versions = [v for (v,) in conn.execute(text("SELECT version FROM schema_migrations ORDER BY version"))]
        assert versions == [f"V{n:03}" for n in range(1, 36)]
        assert conn.execute(text(f"SELECT count(*) FROM {app_ledger.RENUMBERED_TABLE}")).scalar_one() == 0
        assert conn.execute(text(f"SELECT count(*) FROM {app_ledger.SHARED_SLOT_TABLE}")).scalar_one() == 0
    assert diff(fingerprint(engine, schema), target) == {}


def test_a_team_line_database_keeps_its_records_and_gets_the_rebuild_migrations(pg, target, tmp_path, monkeypatch):
    schema, engine = pg("team")
    old_module(TEAM_LINE, tmp_path, "migrate", monkeypatch, "get_engine", engine).run_migrations()
    seed(engine, authority=True)
    before = snapshot(engine, schema)
    assert [tuple(r[:2]) for r in before[0]][19:] == [
        ("V020", "doc authority resolution"), ("V021", "doc authority skipped"), ("V022", "weknora index"),
        ("V023", "identity evidence")]

    run_today(monkeypatch, engine)
    run_today(monkeypatch, engine)
    same_rows(engine, before)
    with engine.begin() as conn:
        renumbered = {tuple(r) for r in conn.execute(text(
            f"SELECT recorded_version,recorded_description,current_file FROM {app_ledger.RENUMBERED_TABLE}"))}
        assert renumbered == {("V020", "doc authority resolution", "V033__doc_authority_resolution.sql"),
                              ("V021", "doc authority skipped", "V034__doc_authority_skipped.sql"),
                              ("V022", "weknora index", "V020__weknora_index.sql"),
                              ("V023", "identity evidence", "V035__identity_evidence.sql")}
        shared = [tuple(r) for r in conn.execute(text(
            f"SELECT version,description,slot_held_by FROM {app_ledger.SHARED_SLOT_TABLE} ORDER BY version"))]
        assert shared == [("V021", "history version rank", "doc authority skipped"),
                          ("V022", "registry changes", "weknora index"),
                          ("V023", "derived clean text", "identity evidence")]
        versions = [v for (v,) in conn.execute(text("SELECT version FROM schema_migrations ORDER BY version"))]
        assert versions == [f"V{n:03}" for n in range(1, 33)]  # V033-V035 ran as the team's V020/V021/V023
    assert diff(fingerprint(engine, schema), target) == {}


@pytest.mark.parametrize("row,expected", [
    (("V021", "identity evidence", "0" * 32), "not a migration any commit"),  # known name, other content
    (("V036", "from another branch", "1" * 32), "not a migration any commit"),
    (("V010", "add composite indexes", "56cc579e4b9f41357f9d41f5532d91b1"), "earlier migration"),  # superseded
])
def test_an_unknown_or_superseded_record_is_refused_and_nothing_is_written(pg, monkeypatch, row, expected):
    schema, engine = pg("conflict")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, description TEXT DEFAULT '', "
                          "checksum TEXT DEFAULT '', applied_at TIMESTAMPTZ DEFAULT NOW())"))
        conn.execute(text("INSERT INTO schema_migrations(version,description,checksum) VALUES (:v,:d,:c)"),
                     dict(zip("vdc", row)))
        before = [tuple(r) for r in conn.execute(text("SELECT * FROM schema_migrations"))]
    with pytest.raises(app_ledger.MigrationStateError) as refused:
        run_today(monkeypatch, engine)
    assert expected in str(refused.value) and row[0] in str(refused.value)
    with engine.begin() as conn:
        assert tables(conn, schema) == {"schema_migrations"}  # nothing created
        assert [tuple(r) for r in conn.execute(text("SELECT * FROM schema_migrations"))] == before


def test_the_known_migration_table_covers_every_file_today():
    from store.migrations.known_migrations import KNOWN

    current = {(row[0], row[1], row[2]) for row in KNOWN if row[3] == "current"}
    for path in sorted(manifests.VERSIONS_DIR.glob("V*.sql")):
        version, description = app_ledger._parse_filename(path)
        assert (version, description, app_ledger._checksum(path.read_text(encoding="utf-8"))) in current, path.name
    assert json.dumps(KNOWN)  # plain data
