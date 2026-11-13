"""The read modes a source supports are declared by the plugin and narrowed per source; nothing is hand-listed twice."""

import dataclasses

import pytest

from fetch import catalog
from fetch.catalog import FETCHABLE, PLUGIN_READ_MODES, READ_MODES, SOURCE_READ_MODES, SPECS, spec
from fetch.sources import PLUGINS
from store.history_models import FetchRequest


def test_every_fetchable_source_declares_modes_from_the_vocabulary_within_its_plugin():
    for item in SPECS:
        if item.fetchable:
            assert item.read_modes and set(item.read_modes) <= set(READ_MODES), item.source_id
            assert set(item.read_modes) <= set(PLUGIN_READ_MODES[item.plugin]), item.source_id
        else:
            assert item.read_modes == (), item.source_id


def test_the_registered_plugins_and_their_declared_modes_are_the_same_set():
    assert set(PLUGIN_READ_MODES) == set(PLUGINS)


@pytest.mark.parametrize("source,modes", [
    ("clinicaltrials", ("query", "ids")), ("pubmed", ("query", "ids")), ("europe_pmc", ("query", "ids")),
    ("fda_crl", ("query",)), ("who_don", ("query", "ids", "window")), ("sec_companyfacts", ("ids",)),
    ("gdelt_doc", ("query", "window")), ("local_files", ("upload",)), ("nmpa", ("ids", "list_pages")),
])
def test_the_modes_of_known_sources_follow_what_their_readers_do(source, modes):
    assert spec(source).read_modes == modes


def test_window_is_declared_exactly_where_the_fetch_request_accepts_an_update_window():
    """FetchRequest accepts updated_since/updated_until only for these sources; the declaration agrees."""
    accepted = {s for s in FETCHABLE if _accepts_window(s)}
    assert accepted == {s for s in FETCHABLE if "window" in spec(s).read_modes} == {"who_don", "gdelt_doc"}


def _accepts_window(source_id):
    from datetime import datetime, timezone

    try:
        FetchRequest(source_id=source_id, query="*", updated_since=datetime(2026, 1, 1, tzinfo=timezone.utc))
    except ValueError:
        return False
    return True


def with_specs(monkeypatch, *replacements):
    changed = {item.source_id: item for item in replacements}
    monkeypatch.setattr(catalog, "SPECS", tuple(changed.get(item.source_id, item) for item in SPECS))


def test_a_connected_source_without_a_mode_fails_the_check(monkeypatch):
    with_specs(monkeypatch, dataclasses.replace(spec("pubmed"), read_modes=()))
    with pytest.raises(ValueError, match="pubmed 已接通但没有读取方式"):
        catalog._check_read_modes()


def test_a_mode_beyond_the_plugin_fails_the_check(monkeypatch):
    with_specs(monkeypatch, dataclasses.replace(spec("fda_crl"), read_modes=("query", "window")))
    with pytest.raises(ValueError, match="fda_crl 的读取方式.*超出插件"):
        catalog._check_read_modes()


def test_an_unconnected_source_with_modes_fails_the_check(monkeypatch):
    with_specs(monkeypatch, dataclasses.replace(spec("ema"), read_modes=("query",)))
    with pytest.raises(ValueError, match="ema 未接通却声明了读取方式"):
        catalog._check_read_modes()


def test_a_narrowing_for_an_unknown_source_fails_the_check(monkeypatch):
    monkeypatch.setitem(SOURCE_READ_MODES, "no_such_source", ("query",))
    with pytest.raises(ValueError, match="no_such_source"):
        catalog._check_read_modes()
