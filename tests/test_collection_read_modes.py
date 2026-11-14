"""The read modes each source declares in the catalog follow what its reader does."""

import pytest

from fetch.catalog import FETCHABLE, spec
from store.record_models import FetchRequest


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
