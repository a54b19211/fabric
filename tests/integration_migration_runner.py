"""The migration runner on real PostgreSQL (throwaway schemas): a fresh build, the earlier ledgers, a new file."""

import os
import shutil
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from store.migrations import runner


@pytest.fixture
def pg():
    """schema(label) -> (name, engine bound to a new schema of the isolated test database); dropped afterwards."""
    url = os.environ.get("DATAFABRIC_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL URL required")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.port == 18436 and parsed.database == "zk_fabric_test"
    admin = create_engine(url)
    created = []

    def schema(label):
        name = "mig_" + label + "_" + uuid4().hex[:12]
        with admin.begin() as conn:
            conn.execute(text("CREATE SCHEMA " + name))
        created.append(name)
        return name, create_engine(url, connect_args={"options": "-csearch_path=" + name + ",public"})

    yield schema
    with admin.begin() as conn:
        for name in created:
            conn.execute(text("DROP SCHEMA " + name + " CASCADE"))
    admin.dispose()


def test_a_fresh_database_gets_every_file_once_and_a_new_file_later(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "VERSIONS", shutil.copytree(runner.VERSIONS, tmp_path / "versions"))
    _, fabric = pg("fabric")
    assert runner.apply("fabric", fabric) == [p.name for p in runner.files_for("fabric")]
    assert runner.apply("fabric", fabric) == [] and runner.plan("fabric", fabric) == []
    (runner.VERSIONS / "V999__synthetic.sql").write_text("CREATE TABLE synthetic_v999 (id int);", encoding="utf-8")
    assert runner.plan("fabric", fabric) == ["V999__synthetic.sql"]
    assert runner.apply("fabric", fabric) == ["V999__synthetic.sql"]
    assert runner.apply("fabric", fabric) == []

    _, app = pg("app")
    assert len(runner.apply("app", app)) == 35  # V001-V035, V008-V011 with their own BEGIN/COMMIT among them
    assert runner.apply("app", app) == []
    with app.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM schema_migrations")).scalar_one() == 35
    with pytest.raises(runner.MigrationError, match="ledger is empty"):  # the app role ran V019/V026 here already
        runner.apply("publication", app)


def test_what_the_earlier_runners_recorded_is_not_run_again(pg):
    _, fabric = pg("adopted")
    files = runner.files_for("fabric")
    with fabric.begin() as conn:  # the ledger as the former --adopt-baseline left it, without the last file
        conn.exec_driver_sql(runner.EVENT_LEDGER)
        for path in files[:-1]:
            conn.execute(text(f"INSERT INTO {runner.EVENTS} (db_role, version, file, event, checksum_md5, sha256, "
                              "actor) VALUES ('history', :v, :f, 'observed_baseline', '', '', 'test')"),
                         {"v": runner.version(path), "f": path.name})
    assert runner.plan("fabric", fabric) == [files[-1].name]

    _, app = pg("team")
    team = {"V020": "doc authority resolution", "V021": "doc authority skipped", "V022": "weknora index",
            "V023": "identity evidence"}  # the team line's numbers for today's V033, V034, V020, V035
    with app.begin() as conn:
        conn.exec_driver_sql(runner.APP_LEDGER)
        for path in runner.files_for("app")[:19]:
            team[runner.version(path)] = runner.description(path)
        for number, description in team.items():
            conn.execute(text("INSERT INTO schema_migrations (version, description) VALUES (:v, :d)"),
                         {"v": number, "d": description})
    assert runner.plan("app", app) == [p.name for p in runner.files_for("app")[20:32]]  # V021-V032
