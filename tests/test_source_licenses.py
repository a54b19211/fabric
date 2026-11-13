"""The licence declarations are the single authority of the source catalog, and are validated against the others."""

import dataclasses

import pytest

from fetch import retired_sources, source_licenses
from fetch.catalog import FETCHABLE, SITE_SOURCES
from fetch.source_licenses import COLLECTED, DERIVED, LicenseSpec, collected_licenses, validate_declarations
from fetch.sources import SOURCE_CATALOG


def test_every_fetchable_collection_spec_has_exactly_one_licence_and_nothing_else_does():
    licenses = collected_licenses()
    assert set(licenses) == set(FETCHABLE)
    assert len(COLLECTED) == len({c.source_id for c in COLLECTED})  # no source declared twice
    assert {c.source_id for c in COLLECTED} | SITE_SOURCES == set(FETCHABLE)


def test_the_catalog_is_exactly_the_collected_and_derived_declarations_and_holds_no_retired_source():
    assert set(SOURCE_CATALOG) == set(FETCHABLE) | {d.source_id for d in DERIVED}
    assert not set(SOURCE_CATALOG) & set(retired_sources.RETIRED)
    for source_id, entry in SOURCE_CATALOG.items():
        declared = collected_licenses().get(source_id) or next((d for d in DERIVED if d.source_id == source_id), None)
        if declared is not None:
            assert entry == declared.catalog_entry()


def test_every_declaration_is_academic_use_only_and_has_a_record_kind():
    for item in [*collected_licenses().values(), *DERIVED]:
        assert item.academic_use_only is True
        assert item.record_kind in ("original", "graph")
    assert {d.source_id: d.record_kind for d in DERIVED} == {
        "reviewed_graph": "graph", "primekg_graph": "graph", "external_archive": "original"}


def test_the_site_licence_is_generated_from_the_collection_spec():
    from fetch.catalog import spec

    for source_id in sorted(SITE_SOURCES):
        item = collected_licenses()[source_id]
        assert item.name == spec(source_id).name and item.business_types == tuple(spec(source_id).evidence_types)
        assert item.acquisition_channel == "site_plugin" and item.access == "explicit_source_allowlist"


def test_validation_passes_on_the_real_declarations():
    validate_declarations()


def test_a_fetchable_source_without_a_licence_fails_validation():
    licenses = collected_licenses()
    del licenses["pubmed"]
    with pytest.raises(ValueError, match="pubmed: fetchable in the collection catalog but has no LicenseSpec"):
        validate_declarations(licenses)


def test_a_licence_for_a_source_nothing_can_fetch_fails_validation():
    licenses = collected_licenses()
    licenses["ema"] = dataclasses.replace(licenses["pubmed"], source_id="ema")  # a declared, unconnected source
    with pytest.raises(ValueError, match="ema: has a LicenseSpec in COLLECTED but the collection catalog cannot"):
        validate_declarations(licenses)


def test_a_source_in_two_groups_fails_validation(monkeypatch):
    clash = dataclasses.replace(DERIVED[0], source_id="pubmed")
    monkeypatch.setattr(source_licenses, "DERIVED", (*DERIVED, clash))
    with pytest.raises(ValueError, match="pubmed: declared as collected and derived"):
        validate_declarations()
    monkeypatch.setattr(source_licenses, "DERIVED", DERIVED)
    monkeypatch.setattr(retired_sources, "RETIRED", {**retired_sources.RETIRED,
                                                      "reviewed_graph": retired_sources.RETIRED["legacy_documents"]})
    with pytest.raises(ValueError, match="reviewed_graph: declared as derived and retired"):
        validate_declarations()


def test_a_collected_source_cannot_be_a_graph_source():
    licenses = collected_licenses()
    licenses["pubmed"] = dataclasses.replace(licenses["pubmed"], record_kind="graph")
    with pytest.raises(ValueError, match="pubmed: a collected source cannot be a graph source"):
        validate_declarations(licenses)


def test_the_declaration_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        COLLECTED[0].name = "other"
    assert isinstance(COLLECTED[0], LicenseSpec)
