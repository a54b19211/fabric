"""GET /api/fabric/sources against a migrated fabric library: readable sources, retired ones, the radar's capabilities;
the source switches (/admin/sources, V047)."""

import json

import pytest
from fastapi import HTTPException
from integration_migration_runner import pg  # noqa: F401 - fixture
from sqlalchemy import text

from api.routers import fabric as router
from fetch import source_refresh
from fetch.catalog import RETIRED
from fetch.sources import SOURCE_CATALOG
from store import records as store
from store.migrations import runner


@pytest.fixture
def library(pg, monkeypatch):
    _, engine = pg("listing")
    monkeypatch.setattr(store, "fabric_engine", lambda: engine)
    runner.apply("fabric", store.fabric_engine())
    with engine.begin() as conn:
        conn.execute(text("UPDATE fabric_sources SET read_enabled=true"))
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
        conn.execute(text("UPDATE fabric_sources SET read_enabled=false WHERE source_id='pubmed'"))
    assert "pubmed" not in by_id(store.sources_receipt()["sources"])


def test_switching_off_public_reads_keeps_only_an_upload(library, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_FETCH_ENABLED", "false")
    assert caps("pubmed")["fetch"] is None
    assert caps("local_files")["fetch"] == {"modes": ["upload"]}


def test_a_new_library_starts_with_the_default_closed_sources_unreadable(pg, monkeypatch):
    _, engine = pg("defaults")
    monkeypatch.setattr(store, "fabric_engine", lambda: engine)
    runner.apply("fabric", engine)
    rows = by_id(router.admin_sources()["sources"])
    assert rows["nccn"]["read_enabled"] is False and rows["pubmed"]["read_enabled"] is False
    assert rows["clinicaltrials"]["read_enabled"] is True and rows["nccn"]["fetch_enabled"] is True
    assert rows["clinicaltrials"]["latest_run"] is None


def test_a_switch_change_is_logged_and_stops_the_scheduled_fetch(library, monkeypatch):
    body = router.SourceSwitches(fetch_enabled=False, changed_by="host")
    changed = router.admin_source_switches("who_don", body)
    assert (changed["read_enabled"], changed["fetch_enabled"], changed["changed_by"]) == (True, False, "host")
    assert by_id(router.admin_sources()["sources"])["who_don"]["last_change"]["changed_by"] == "host"
    monkeypatch.setenv("DATAFABRIC_WATCHES", json.dumps([
        {"id": "who", "request": {"source_id": "who_don", "query": "*"}, "cadence_seconds": 900}]))
    assert source_refresh.refresh_once() == [{"watch_id": "who", "state": "disabled"}]
    with pytest.raises(HTTPException) as missing:
        router.admin_source_switches("no_such_source", router.SourceSwitches(read_enabled=True))
    assert missing.value.status_code == 404

