"""SOURCE_CATALOG is pinned: the licence catalog Fabric publishes in /api/history/sources, source by source.

The golden file was produced from the hand-written catalog of 1df47aa (before the catalog became generated from
declarations) and changed once, on purpose, when legacy_documents left the catalog and the site sources stopped saying
that documents are frozen into it. Changing the text of a source means changing the golden file in the same commit."""

import json
from pathlib import Path

from fetch.sources import SOURCE_CATALOG

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "source_catalog_golden.json").read_text(encoding="utf-8"))


def test_the_catalog_has_exactly_the_golden_sources():
    assert sorted(SOURCE_CATALOG) == sorted(GOLDEN)


def test_every_source_entry_is_the_golden_one_field_by_field():
    for source_id, expected in GOLDEN.items():
        assert json.loads(json.dumps(SOURCE_CATALOG[source_id])) == expected, source_id


def test_the_catalog_keys_keep_their_order():
    """The seed order and the order of equal-sorted listings follow it."""
    assert list(SOURCE_CATALOG) == list(GOLDEN)
