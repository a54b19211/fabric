"""The processor registry: versions, and the sources each processor applies to, in one place."""

from clean import (
    attachment_parse,
    clean_stage,
    clean_text,
    measure_extract,
    processors,
    profile,
    registry_changes,
    registry_diff,
)
from fetch.catalog import ATTACHMENT_SOURCES, CATALOG


def test_versions_are_the_modules_versions():
    assert {p.name: p.version for p in processors.PROCESSORS} == {
        "clean_text": clean_text.PROCESSOR_VERSION, "attachment_parse": attachment_parse.PROCESSOR_VERSION,
        "registry_diff": registry_diff.PROCESSOR_VERSION, "profile": profile.PROCESSOR_VERSION,
        "measure_extract": measure_extract.PROCESSOR_VERSION}


def test_applicability_is_a_predicate_over_the_source_not_a_second_list():
    for source in CATALOG:
        applied = processors.applicable(source)
        assert ("attachment_parse" in applied) == (source in ATTACHMENT_SOURCES)
        assert ("registry_diff" in applied) == (source == "clinicaltrials")
        assert "clean_text" in applied
    assert processors.applicable("reviewed_graph") == {} == processors.applicable("primekg_graph")


def test_the_processing_stages_use_the_registry_names():
    assert registry_changes.DIFF_SOURCES is processors.REGISTRY_DIFF_SOURCES
    assert clean_stage.SKIP_SOURCES is processors.UNCLEANED_SOURCES


def test_a_new_attachment_source_gets_the_attachment_processor_without_another_edit(monkeypatch):
    import fetch.catalog as catalog

    monkeypatch.setattr(catalog, "ATTACHMENT_SOURCES", ATTACHMENT_SOURCES | {"brand_new_site"})
    assert "attachment_parse" in processors.applicable("brand_new_site")


def test_measurements_are_read_from_the_declared_sources_only():
    assert processors.MEASURE_SOURCES == ("who_don",)
    assert processors.applicable("who_don")["measure_extract"] == "datafabric.measure_extract.v6"
    assert "measure_extract" not in processors.applicable("clinicaltrials")
    assert "measure_extract" not in processors.applicable("reviewed_graph")
