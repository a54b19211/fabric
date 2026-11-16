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
