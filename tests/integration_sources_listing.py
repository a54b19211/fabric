"""The source list Fabric publishes (GET /api/history/sources), against a migrated History library: descriptions from the
declarations, licence state from the library, per-source capabilities from both, the two digests, retired sources."""

import json
import re

import pytest
from integration_migration_runner import pg  # noqa: F401 - fixture
from sqlalchemy import text

from api.routers import history as router
from clean import processors
from fetch import catalog
from fetch import source_capabilities as caps
from fetch.sources import SOURCE_CATALOG
from store import history as store
from store.migrations import runner

DERIVED_GRAPHS = {"reviewed_graph", "primekg_graph"}
TARGETS = [{"source_instance": "synthetic.index.pg", "source_id": source, "is_demo": False,
            "knowledge_base_id": "kb-" + source, "source_scope": "research"}
           for source in ("external_archive", "local_files", "clinicaltrials", "reviewed_graph")]


@pytest.fixture
def library(pg, monkeypatch):
    _, engine = pg("listing")
    monkeypatch.setattr(store, "history_engine", lambda: engine)
    runner.apply("history", store.history_engine())
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", json.dumps(sorted([*SOURCE_CATALOG, "legacy_documents"])))
    monkeypatch.setenv("DATAFABRIC_INDEX_TARGETS", json.dumps(TARGETS))
    with engine.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=true, published_seq=3"))
    return engine


def by_id(items):
    return {item["source_id"]: item for item in items}


def listing():
    return store.sources_receipt()


# ---- what is listed, and where ----

def test_every_collected_and_derived_source_is_listed_with_its_declared_text_and_the_library_state(library):
    receipt = listing()
    listed = by_id(receipt["sources"])
    assert sorted(listed) == sorted(SOURCE_CATALOG)
    for source_id, item in listed.items():
        text_of = {key: value for key, value in item.items() if key in SOURCE_CATALOG[source_id]}
        assert json.loads(json.dumps(text_of)) == json.loads(json.dumps(SOURCE_CATALOG[source_id])), source_id
        assert item["published_seq"] == 3 and item["read_enabled"] is True and item["academic_use_only"] is True


def test_the_sources_array_is_sorted_by_id_like_the_library_listing(library):
    ids = [item["source_id"] for item in listing()["sources"]]
    assert ids == sorted(ids)


def test_the_retired_bridge_is_outside_sources_and_inside_retired_sources_with_its_lifecycle(library):
    receipt = listing()
    assert "legacy_documents" not in by_id(receipt["sources"])
    (retired,) = receipt["retired_sources"]
    assert retired["source_id"] == "legacy_documents" and retired["readable"] is True
    assert retired["resolves_via"] == "legacy_aliases"
    assert retired["retirement"]["state"] == "decided" and retired["retirement"]["effective_at"] is None
    assert retired["retirement"]["decided_at"]["precision"] == "day"
    assert retired["retirement"]["decision_date"] == "2026-11-06"
    flags = retired["capabilities"]
    assert flags["readable"] and flags["read"] and not (flags["list"] or flags["search"] or flags["select"]
                                                        or flags["fetch"] or flags["graph"])


def test_the_effective_time_shows_up_once_the_deployment_wrote_it(library):
    from ops import source_lifecycle

    with library.begin() as conn:
        source_lifecycle.record(conn, "legacy_documents", "effective", actor="deploy:test")
    (retired,) = listing()["retired_sources"]
    assert retired["retirement"]["state"] == "effective" and retired["retirement"]["effective_at"]
    assert retired["retirement"]["effective_at"] > retired["retirement"]["decided_at"]["at"]


def test_the_transition_setting_repeats_the_retired_bridge_in_sources_without_giving_it_a_capability(library, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_RETIRED_IN_SOURCES", "legacy_documents")
    receipt = listing()
    entry = by_id(receipt["sources"])["legacy_documents"]
    assert entry["capabilities"]["select"] is False and entry["capabilities"]["retired"] is True
    assert receipt["retired_sources"][0]["source_id"] == "legacy_documents"


def test_a_source_that_is_not_readable_is_not_in_sources_but_is_reported_as_unavailable(library):
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='pubmed'"))
    receipt = listing()
    assert "pubmed" not in by_id(receipt["sources"])  # an older consumer reading only `sources` sees it gone
    item = by_id(receipt["unavailable_sources"])["pubmed"]
    flags = item["capabilities"]
    assert flags["readable"] is False and not any(flags[k] for k in ("read", "list", "search", "select", "graph", "watch"))
    assert flags["fetch"] is None and flags["attachments"] is None and flags["index"]["searchable"] is False
    assert "pubmed" not in set(receipt["readable_source_ids"])


def test_readable_is_the_librarys_current_decision_for_every_row_not_derived_from_selection(library):
    """N14: revoking a source a graph depends on makes the graph unreadable, and it was never selectable."""
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET catalog = catalog || CAST(:d AS jsonb) "
                          "WHERE source_id='reviewed_graph'"), {"d": json.dumps({"dependency_source_ids": ["pubmed"]})})
    before = by_id(listing()["sources"])["reviewed_graph"]["capabilities"]
    assert before["readable"] and before["graph"] and before["select"] is False
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='pubmed'"))
    receipt = listing()
    assert "reviewed_graph" not in by_id(receipt["sources"])
    assert by_id(receipt["unavailable_sources"])["reviewed_graph"]["capabilities"]["readable"] is False


def test_the_retired_bridge_stops_being_readable_when_its_original_site_is_revoked(library):
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET catalog = catalog || CAST(:d AS jsonb) "
                          "WHERE source_id='legacy_documents'"), {"d": json.dumps({"dependency_source_ids": ["nmpa"]})})
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='nmpa'"))
    (retired,) = listing()["retired_sources"]
    assert retired["readable"] is False and retired["capabilities"]["readable"] is False
    assert retired["capabilities"]["read"] is False


# ---- capabilities per source (P1, P7) ----

def expected_flags(source_id, library_has_versions=True):
    spec = catalog.CATALOG.get(source_id)
    graph = source_id in DERIVED_GRAPHS
    retired = source_id == "legacy_documents"
    indexed = source_id in {t["source_id"] for t in TARGETS} and not graph
    return {"read": library_has_versions, "list": not retired, "search": indexed and not retired,
            "select": not graph and not retired, "graph": graph,
            "watch": bool(spec and spec.watchable)}


@pytest.mark.parametrize("source_id", sorted([*SOURCE_CATALOG, "legacy_documents"]))
def test_each_sources_capabilities_follow_the_declarations(library, source_id):
    receipt = listing()
    entry = by_id(receipt["sources"] + receipt["retired_sources"])[source_id]
    flags = entry["capabilities"]
    assert flags["readable"] is True
    assert {k: flags[k] for k in ("read", "list", "search", "select", "graph", "watch")} == expected_flags(source_id)
    spec = catalog.CATALOG.get(source_id)
    assert (flags["fetch"] or {}).get("modes") == (list(spec.read_modes) if spec else None)
    if flags["fetch"]:
        assert flags["fetch"]["admin_only"] is True
    applied = processors.applicable(source_id)
    assert (flags["attachments"] or {}).get("text_processor") == applied.get("attachment_parse")
    assert (flags["clean_text"] or {}).get("processor") == applied.get("clean_text")
    assert (flags["registry_diff"] or {}).get("processor") == applied.get("registry_diff")
    assert flags["material_kind"] == ("fabric_document_evidence" if source_id not in DERIVED_GRAPHS else None)
    expected_targets = [{"scope": "research", "is_demo": False}] if source_id in {t["source_id"] for t in TARGETS} else []
    assert flags["index"]["targets"] == expected_targets
    assert flags["index"]["searchable"] is flags["search"]
    assert ("public_history_reconstruction" in flags["history_modes"]) is (
        source_id not in caps.RECONSTRUCTION_UNSUPPORTED)


def test_the_controlled_archive_and_local_files_keep_read_search_and_select(library):
    listed = by_id(listing()["sources"])
    archive, local = listed["external_archive"]["capabilities"], listed["local_files"]["capabilities"]
    for flags in (archive, local):
        assert flags["read"] and flags["list"] and flags["search"] and flags["select"]
        assert not flags["graph"]
    assert archive["fetch"] is None  # a fixed archive is not crawled
    assert local["fetch"] == {"modes": ["upload"], "admin_only": True}  # a consumer's query-fetch does not match "upload"


def test_a_graph_package_is_read_through_the_graph_interface_not_offered_as_a_selectable_source(library):
    for source_id in DERIVED_GRAPHS:
        flags = by_id(listing()["sources"])[source_id]["capabilities"]
        assert flags["graph"] and flags["read"] and flags["list"]
        assert not (flags["select"] or flags["search"] or flags["fetch"])


def test_a_source_without_versions_cannot_be_read_yet(library):
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET published_seq=0 WHERE source_id='pubmed'"))
    flags = by_id(listing()["sources"])["pubmed"]["capabilities"]
    assert flags["read"] is False and flags["list"] and flags["select"]


def test_switching_off_public_reads_removes_every_mode_that_reaches_outside_but_not_an_upload(library, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "false")
    listed = by_id(listing()["sources"])
    assert listed["pubmed"]["capabilities"]["fetch"] is None
    assert listed["local_files"]["capabilities"]["fetch"]["modes"] == ["upload"]


def test_a_broken_index_configuration_is_reported_per_source_and_the_list_still_works(library, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_TARGETS", "not json")
    index = by_id(listing()["sources"])["external_archive"]["capabilities"]["index"]
    assert index == {"targets": [], "searchable": False, "configuration_error": "index_target_configuration"}


# ---- descriptions are the code's, decisions are the library's (P2) ----

def test_a_stale_description_in_the_library_does_not_reach_the_list_but_the_administrators_decision_does(library):
    stale = {"name": "旧说明", "acquisition_channel": "legacy_database", "access": "kept",
             "read_scope": "旧范围", "limitations": ["旧"]}
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET catalog = catalog || CAST(:m AS jsonb) WHERE source_id='nmpa'"),
                     {"m": json.dumps(stale)})
    item = by_id(listing()["sources"])["nmpa"]
    assert item["name"] == SOURCE_CATALOG["nmpa"]["name"] and item["acquisition_channel"] == "site_plugin"
    assert item["read_scope"] == SOURCE_CATALOG["nmpa"]["read_scope"]
    assert item["access"] == "kept"  # the administrator's decision in the row wins


def test_seeding_never_rewrites_an_existing_row(library):
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET catalog = catalog || CAST(:m AS jsonb) WHERE source_id='pubmed'"),
                     {"m": json.dumps({"name": "snapshot"})})
    runner.apply("history", store.history_engine())
    with library.begin() as conn:
        assert conn.execute(text("SELECT catalog->>'name' FROM history_sources WHERE source_id='pubmed'")
                            ).scalar_one() == "snapshot"


# ---- the two digests (definition vs receipt) ----

def test_the_definition_digest_depends_on_the_code_only_and_the_receipt_digest_on_everything(library):
    first = listing()
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET published_seq=99 WHERE source_id='pubmed'"))
    second = listing()
    assert re.fullmatch(r"[0-9a-f]{64}", first["definition_sha256"])
    assert first["definition_sha256"] == second["definition_sha256"] == caps.definition_sha256()
    assert first["receipt_sha256"] != second["receipt_sha256"]  # licence state and progress are in the receipt


def test_the_receipt_digest_covers_the_receipt_without_itself(library):
    receipt = listing()
    body = {k: v for k, v in receipt.items() if k not in ("receipt_sha256", "readable_source_ids")}
    assert caps.digest(body) == receipt["receipt_sha256"] == caps.receipt_sha256(receipt)


def test_the_definition_digest_changes_when_a_declaration_changes(monkeypatch):
    import dataclasses

    before = caps.definition_sha256()
    monkeypatch.setattr(catalog, "SPECS", tuple(
        dataclasses.replace(s, read_modes=("query",)) if s.source_id == "pubmed" else s for s in catalog.SPECS))
    assert caps.definition_sha256() != before


# ---- the HTTP shape ----

def test_the_endpoint_returns_business_types_the_old_fields_and_the_new_ones(library):
    body = router.sources()
    assert {"business_types", "sources", "capabilities_version", "definition_sha256", "receipt_sha256",
            "retired_sources", "unavailable_sources"} <= set(body)
    assert "readable_source_ids" not in body
    assert body["capabilities_version"] == "datafabric.source_capabilities.v1"
    assert [item["source_id"] for item in body["sources"]] == sorted(item["source_id"] for item in body["sources"])
    assert all(isinstance(item["source_id"], str) and item["capabilities"]["readable"] for item in body["sources"])


def test_a_source_the_allowlist_does_not_name_or_that_is_disabled_is_not_in_sources(library, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["pubmed","clinicaltrials"]')
    with library.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='pubmed'"))
    assert [item["source_id"] for item in store.list_sources()] == ["clinicaltrials"]
