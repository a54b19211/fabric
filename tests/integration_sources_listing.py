"""The source list Fabric publishes (store.list_sources), against a migrated History library."""

import json

import pytest
from integration_migration_baselines import pg  # noqa: F401 - fixture
from sqlalchemy import text

from biz_rules.history_sources import SOURCE_CATALOG
from db import history as store
from migration import history as migration


@pytest.fixture
def library(pg, monkeypatch):
    _, engine = pg("listing")
    monkeypatch.setattr(store, "history_engine", lambda: engine)
    monkeypatch.setattr(migration, "history_engine", lambda: engine)
    migration.migrate_history()
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", json.dumps(sorted(SOURCE_CATALOG)))
    with engine.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=true"))
    return engine


def test_every_catalog_source_is_listed_with_its_catalog_text_and_the_library_state(library):
    listed = store.list_sources()
    assert [item["source_id"] for item in listed] == sorted(SOURCE_CATALOG)
    for item in listed:
        expected = {"source_id": item["source_id"], **SOURCE_CATALOG[item["source_id"]],
                    "published_seq": 0, "read_enabled": True}
        assert json.loads(json.dumps(item)) == json.loads(json.dumps(expected)), item["source_id"]


def test_a_source_the_allowlist_does_not_name_or_that_is_disabled_is_not_listed(library, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["pubmed","clinicaltrials"]')
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='pubmed'"))
    assert [item["source_id"] for item in store.list_sources()] == ["clinicaltrials"]


def test_a_derived_source_disappears_when_a_dependency_is_revoked(library):
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET catalog = catalog || CAST(:d AS jsonb) "
                          "WHERE source_id='reviewed_graph'"), {"d": json.dumps({"dependency_source_ids": ["pubmed"]})})
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='pubmed'"))
    ids = [item["source_id"] for item in store.list_sources()]
    assert "reviewed_graph" not in ids and "pubmed" not in ids and "clinicaltrials" in ids
