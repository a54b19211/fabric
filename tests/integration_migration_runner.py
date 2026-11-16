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
