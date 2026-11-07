"""The processor registry: versions, and the sources each processor applies to, in one place."""

from collection.catalog import ATTACHMENT_SOURCES, CATALOG
from db import clean_stage, registry_changes
from processing import attachment_parse, clean_text, profile, registry, registry_diff


def test_versions_are_the_modules_versions():
    assert {p.name: p.version for p in registry.PROCESSORS} == {
        "clean_text": clean_text.PROCESSOR_VERSION, "attachment_parse": attachment_parse.PROCESSOR_VERSION,
        "registry_diff": registry_diff.PROCESSOR_VERSION, "profile": profile.PROCESSOR_VERSION}


def test_applicability_is_a_predicate_over_the_source_not_a_second_list():
    for source in CATALOG:
        applied = registry.applicable(source)
        assert ("attachment_parse" in applied) == (source in ATTACHMENT_SOURCES)
        assert ("registry_diff" in applied) == (source == "clinicaltrials")
        assert "clean_text" in applied
    assert registry.applicable("reviewed_graph") == {} == registry.applicable("primekg_graph")


def test_the_processing_stages_use_the_registry_names():
    assert registry_changes.DIFF_SOURCES is registry.REGISTRY_DIFF_SOURCES
    assert clean_stage.SKIP_SOURCES is registry.UNCLEANED_SOURCES


def test_a_new_attachment_source_gets_the_attachment_processor_without_another_edit(monkeypatch):
    import collection.catalog as catalog

    monkeypatch.setattr(catalog, "ATTACHMENT_SOURCES", ATTACHMENT_SOURCES | {"brand_new_site"})
    assert "attachment_parse" in registry.applicable("brand_new_site")


def test_the_definition_lists_each_processor_with_the_sources_it_applies_to():
    definition = {item["name"]: item for item in registry.definition()}
    assert definition["registry_diff"]["sources"] == ["clinicaltrials"]
    assert "reviewed_graph" not in definition["clean_text"]["sources"]
    assert set(definition["attachment_parse"]["sources"]) >= {"pubmed", "local_files", "nmpa"}
