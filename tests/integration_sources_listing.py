"""GET /api/history/sources against a migrated History library: readable sources, retired ones, the radar's capabilities."""

import pytest
from integration_migration_runner import pg  # noqa: F401 - fixture
from sqlalchemy import text

from api.routers import history as router
from fetch.catalog import RETIRED
from fetch.sources import SOURCE_CATALOG
from store import history as store
from store.migrations import runner


@pytest.fixture
def library(pg, monkeypatch):
    _, engine = pg("listing")
    monkeypatch.setattr(store, "history_engine", lambda: engine)
    runner.apply("history", store.history_engine())
    with engine.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=true"))
    return engine


def by_id(items):
    return {item["source_id"]: item for item in items}


def caps(source_id):
    receipt = store.sources_receipt()
    return by_id(receipt["sources"] + receipt["retired_sources"])[source_id]["capabilities"]


def test_readable_sources_are_listed_by_id_and_the_retired_bridge_apart(library):
    body = router.sources()
    assert set(body) == {"capabilities_version", "sources", "retired_sources"}
    assert body["capabilities_version"] == "datafabric.source_capabilities.v1"
    ids = [item["source_id"] for item in body["sources"]]
    assert ids == sorted(ids) and set(ids) == set(SOURCE_CATALOG) - set(RETIRED)
    assert by_id(body["sources"])["nmpa"]["name"] == SOURCE_CATALOG["nmpa"]["name"]
    (retired,) = body["retired_sources"]
    assert retired["source_id"] == "legacy_documents" and retired["capabilities"]["readable"] is True


@pytest.mark.parametrize("source_id,select,modes,attachments,diff", [
    ("clinicaltrials", True, ["query", "ids"], False, True),
    ("nmpa", True, ["ids", "list_pages"], True, False),
    ("local_files", True, ["upload"], True, False),
    ("external_archive", True, None, False, False),
    ("reviewed_graph", False, None, False, False),
    ("legacy_documents", False, None, False, False),
])
def test_the_capabilities_the_radar_reads(library, source_id, select, modes, attachments, diff):
    flags = caps(source_id)
    assert flags["readable"] is True and flags["select"] is select
    assert (flags["fetch"] or {}).get("modes") == modes
    assert (flags["attachments"] == {"versions": True}) is attachments
    assert bool(flags["registry_diff"]) is diff


def test_a_disabled_source_is_not_in_sources(library):
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='pubmed'"))
    assert "pubmed" not in by_id(store.sources_receipt()["sources"])


def test_switching_off_public_reads_keeps_only_an_upload(library, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "false")
    assert caps("pubmed")["fetch"] is None
    assert caps("local_files")["fetch"] == {"modes": ["upload"]}
