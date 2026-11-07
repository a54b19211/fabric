"""Capability computation on its own (no database): licence state AND registered implementation."""

import json

from biz_rules import source_capabilities as caps
from biz_rules.source_capabilities import SourceFacts, capabilities


def flags(source_id, **kw):
    return capabilities(SourceFacts(source_id, **{"readable": True, "published_seq": 1, **kw}))


def test_an_unreadable_source_has_no_capability_even_when_the_code_could_do_everything():
    off = flags("clinicaltrials", readable=False, index_targets=({"scope": "research", "is_demo": False},))
    assert off["readable"] is False
    assert not any(off[k] for k in ("read", "list", "search", "select", "graph", "watch"))
    assert off["fetch"] is None and off["attachments"] is None and off["registry_diff"] is None
    assert off["clean_text"] is None and off["material_kind"] is None


def test_a_readable_source_with_an_implementation_gets_the_capabilities_the_implementation_has():
    on = flags("clinicaltrials", index_targets=({"scope": "research", "is_demo": False},))
    assert on["fetch"] == {"modes": ["query", "ids"], "admin_only": True} and on["watch"] is True
    assert on["registry_diff"] == {"processor": "datafabric.registry_diff.v3"}
    assert on["attachments"] is None and on["search"] is True and on["select"] is True


def test_search_needs_an_index_target_a_source_without_one_is_not_searchable():
    assert flags("pubmed")["search"] is False
    assert flags("pubmed", index_targets=({"scope": "research", "is_demo": False},))["search"] is True


def test_a_site_source_has_attachment_text_and_ids_and_list_pages():
    nmpa = flags("nmpa")
    assert nmpa["attachments"] == {"versions": True, "text_processor": "datafabric.attachment_parse.v2"}
    assert nmpa["fetch"]["modes"] == ["ids", "list_pages"]


def test_a_declared_but_unconnected_source_cannot_be_fetched_even_if_the_library_has_it():
    assert flags("ema")["fetch"] is None and flags("ema")["watch"] is False


def test_a_graph_source_is_graph_not_selectable_not_searchable_and_not_cleaned():
    graph = flags("reviewed_graph", index_targets=({"scope": "research", "is_demo": False},))
    assert graph["graph"] and not (graph["select"] or graph["search"]) and graph["clean_text"] is None
    assert graph["record_kind"] == "graph" and graph["material_kind"] is None


def test_the_controlled_archive_is_an_original_source_selectable_and_never_fetched():
    archive = flags("external_archive", index_targets=({"scope": "research", "is_demo": True},))
    assert archive["select"] and archive["search"] and archive["fetch"] is None and not archive["graph"]


def test_a_retired_source_keeps_read_and_loses_list_search_select():
    legacy = flags("legacy_documents", index_targets=({"scope": "research", "is_demo": False},))
    assert legacy["read"] and legacy["retired"]
    assert not (legacy["list"] or legacy["search"] or legacy["select"] or legacy["graph"])


def test_the_public_fetch_switch_closes_outside_modes_and_keeps_an_upload():
    assert flags("pubmed", fetch_enabled=False)["fetch"] is None
    assert flags("local_files", fetch_enabled=False)["fetch"] == {"modes": ["upload"], "admin_only": True}
    assert flags("who_don", fetch_enabled=True)["fetch"]["modes"] == ["query", "ids", "window"]


def test_history_modes_leave_out_reconstruction_where_there_is_no_proof_of_the_past():
    assert "public_history_reconstruction" in flags("pubmed")["history_modes"]
    for source_id in ("reviewed_graph", "primekg_graph", "legacy_documents"):
        assert "public_history_reconstruction" not in flags(source_id)["history_modes"]


def test_reconstruction_support_agrees_with_what_the_read_path_refuses():
    from biz_rules.history_models import HistoryError
    from db.history import _derived_mode

    for source_id in ("pubmed", "reviewed_graph", "primekg_graph", "legacy_documents", "external_archive"):
        try:
            _derived_mode(source_id, "public_history_reconstruction")
            refused = False
        except HistoryError:
            refused = True
        assert refused == (source_id in caps.RECONSTRUCTION_UNSUPPORTED), source_id


def test_the_capabilities_are_plain_data_and_the_definition_digest_is_stable():
    assert json.dumps(flags("pubmed"))
    assert caps.definition_sha256() == caps.definition_sha256()
    assert caps.receipt_sha256({"a": 1, "receipt_sha256": "x"}) == caps.receipt_sha256({"a": 1})
    assert caps.receipt_sha256({"a": 1}) != caps.receipt_sha256({"a": 2})


def test_an_unavailable_entry_names_the_source_with_every_flag_false():
    item = caps.unavailable("pubmed")
    assert item["source_id"] == "pubmed" and item["capabilities"]["readable"] is False


def test_measurements_are_a_capability_of_the_declared_original_sources_only(monkeypatch):
    from processing import registry

    who = flags("who_don")["measurements"]
    assert who == {"processor": "datafabric.measure_extract.v5", "contract": "datafabric.data_objects.v2",
                   "checker": "biocheck.extraction_rules.v17", "check_contract": "extraction_consistency.v3",
                   "history_modes": ["current", "operational_replay", "public_history_reconstruction"]}
    assert flags("who_don", readable=False)["measurements"] is None  # the permission first
    assert flags("pubmed")["measurements"] is None
    # a derived source (a graph package) never gets measurements, even if it were declared
    monkeypatch.setattr(registry, "MEASURE_SOURCES", ("who_don", "reviewed_graph"))
    assert flags("reviewed_graph")["measurements"] is None
