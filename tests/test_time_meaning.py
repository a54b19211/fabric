"""The retrieval metadata keeps every source time and says what it means."""

import json
from datetime import datetime, timezone

from fetch.gdelt_news import article_id
from fetch.projection import project_record
from store import index_publication as index
from store.record_models import ArchiveRecord, AvailabilityProof, VersionRecord, sha256

SEEN = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
ARTIFACT = {"doc_type": "notice", "doc_category": "official", "evidence_types": [],
            "processor_version": "clean_text.synthetic", "text_sha256": "d" * 64}


def version(source_id, record_id, data):
    raw = json.dumps(data)
    projected = project_record(
        source_id, ArchiveRecord(source_record_id=record_id, record_kind="document", raw_content=raw))
    return VersionRecord(
        source_id=source_id, source_record_id=record_id, version_id="a" * 64, is_demo=True, raw_content=raw,
        raw_sha256=sha256(raw), first_seen_at=SEEN, received_at=SEEN,
        availability_proof=AvailabilityProof(kind="unverified"), published_seq=1, **projected)


def test_who_times_keep_their_values_and_say_what_they_are():
    data = {"Id": "00000000-0000-4000-8000-000000000001", "Title": "SYNTHETIC outbreak notice",
            "PublicationDate": "2026-10-10T12:00:00Z", "PublicationDateAndTime": "2026-10-10T11:00:00Z",
            "LastModified": "2026-10-11T12:00:01Z", "Summary": "<p>Declared notice.</p>"}
    meta = index.metadata(version("who_don", data["Id"], data), None, ARTIFACT, [])
    assert (meta["published_at"], meta["published_at_meaning"]) == ("2026-10-10T11:00:00Z", "who_notice_publication")
    assert (meta["source_updated_at"], meta["source_updated_at_meaning"]) == (
        "2026-10-11T12:00:01Z", "who_notice_update")


def test_gdelt_observation_time_is_kept_as_an_observation_not_a_source_update():
    data = {"url": "https://example.org/article?id=1", "title": "SYNTHETIC report", "seendate": "20261030T010000Z"}
    meta = index.metadata(version("gdelt_doc", article_id(data), data), None, ARTIFACT, [])
    assert meta["source_updated_at"].startswith("2026-10-30T01:00:00")
    assert meta["source_updated_at_meaning"] == "gdelt_first_observed"
    assert "published_at" not in meta and "published_at_meaning" not in meta
