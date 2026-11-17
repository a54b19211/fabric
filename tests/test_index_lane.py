"""Which index lane a version goes into (store.index_publication._lane): fresh native versions before the backfill."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from store.index_publication import _lane

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def version(days_ago, **facts):
    return SimpleNamespace(first_seen_at=NOW - timedelta(days=days_ago), facts=facts)


def test_a_freshly_fetched_version_is_new_and_an_old_one_is_backfill():
    assert _lane(version(0), NOW) == "new"
    assert _lane(version(2, role="site_record"), NOW) == "new"
    assert _lane(version(3), NOW) == "backfill"


def test_a_migrated_version_is_backfill_whenever_it_was_imported():
    assert _lane(version(0, publication_role="migrated_record"), NOW) == "backfill"


def test_the_index_body_stops_before_the_first_segment_marked_not_for_the_index():
    from store.index_publication import index_body

    text_ = "# T\n\n## a\n\nkept\n\n## locations\n\nsite list"
    cut = text_.index("## locations")
    segments = [{"start": 0, "end": 3}, {"start": 5, "end": 15}, {"start": cut, "end": len(text_), "index": False}]
    assert index_body(text_, segments) == "# T\n\n## a\n\nkept"
    assert index_body(text_, segments[:2]) == text_


def test_versions_that_differ_only_in_the_tail_left_out_of_the_index_are_one_artifact():
    from store.index_publication import _settled, artifact_key

    head = {"processor_version": "clean.v", "text_sha256": "a" * 64, "body_sha256": "c" * 64}
    older = {"processor_version": "clean.v", "text_sha256": "b" * 64, "body_sha256": "c" * 64}
    assert artifact_key(head) == artifact_key(older) == "clean.v:" + "c" * 64
    assert artifact_key({"processor_version": "clean.v", "text_sha256": "a" * 64}) == "clean.v:" + "a" * 64
    # the bridge's duplicate receipt stops the entry with its reason instead of retrying
    assert _settled({"index_status": "duplicate_content"})["error"]["code"] == "index_duplicate_content"


def test_a_processor_upgrade_that_leaves_the_text_unchanged_is_the_same_text():
    from store.index_publication import same_text

    assert same_text("datafabric.clean_text.v13:" + "a" * 64, "datafabric.clean_text.v14:" + "a" * 64)
    assert not same_text("datafabric.clean_text.v13:" + "a" * 64, "datafabric.clean_text.v14:" + "b" * 64)
    assert not same_text("", "")  # entries from before artifact keys have none
