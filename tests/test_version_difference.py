"""Version differences: which of three causes made a version, from the two stored versions alone."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import history as history_router
from biz_rules import who_don
from biz_rules.gdelt_news import article_id
from biz_rules.history_models import ArchiveRecord, sha256
from biz_rules.history_sources import project_record
from biz_rules.version_difference import PROJECTED_FIELDS, classify
from db import version_difference

WHO_ID = "00000000-0000-4000-8000-000000000001"
UNCHANGED = {"parser_changed": False, "body_changed": False, "last_modified_changed": False, "structure_changed": False}


def notice(**changes):
    return {
        "Id": WHO_ID, "DonId": "SYNTHETIC-DON-1", "Title": "SYNTHETIC outbreak notice",
        "PublicationDate": "2026-10-10T12:00:00Z", "PublicationDateAndTime": "2026-10-10T11:00:00Z",
        "LastModified": "2026-10-11T12:00:01Z", "Summary": "<p>Declared notice.</p>", "UrlName": "synthetic-notice",
        **changes,
    }


def stored(source_id, data, record_id=WHO_ID, *, raw=None):
    """A version as the store keeps it: the original and its projection."""
    raw = json.dumps(data) if raw is None else raw
    item = ArchiveRecord(source_record_id=record_id, record_kind="document", raw_content=raw)
    return {**project_record(source_id, item), "raw_content": raw, "raw_sha256": sha256(raw)}


def test_the_same_original_in_another_serialization_is_serialization_only():
    old = stored("who_don", notice())
    new = stored("who_don", notice(), raw=json.dumps(notice(), sort_keys=True, indent=2))
    assert old["raw_sha256"] != new["raw_sha256"]
    assert classify("who_don", old, new) == {"kind": "serialization_only", **UNCHANGED}


def test_two_captures_of_a_page_that_differ_only_in_html_are_serialization_only():
    def capture(html):
        return {"schema_version": "datafabric.web_capture.v1", "role": "web_capture", "site": "nmpa",
                "url": "https://www.nmpa.gov.cn/xxgk/ggtg/1.html", "html": html, "html_sha256": "h" + html,
                "parsed": {"title": "T", "publish_date": "2026-11-02", "markdown": "# T", "parse_method": "css", "extra": {}},
                "attachments": []}

    url = "https://www.nmpa.gov.cn/xxgk/ggtg/1.html"
    old, new = (stored("nmpa", capture(html), url) for html in ("<a id=aria1>", "<a id=aria2>"))
    assert old["facts"]["html_sha256"] != new["facts"]["html_sha256"]
    assert classify("nmpa", old, new) == {"kind": "serialization_only", **UNCHANGED}


def test_a_parser_upgrade_on_the_same_original_is_projection_only(monkeypatch):
    old = stored("who_don", notice())
    monkeypatch.setattr(who_don, "WHO_PARSER", "datafabric.who.don.projection.v0.3")
    new = stored("who_don", notice())
    assert old["raw_sha256"] == new["raw_sha256"]
    assert classify("who_don", old, new) == {"kind": "projection_only", **UNCHANGED, "parser_changed": True}


def test_a_changed_original_is_raw_changed_and_says_what_moved():
    old = stored("who_don", notice())
    touched = classify("who_don", old, stored("who_don", notice(LastModified="2026-10-12T08:00:00Z")))
    assert touched["kind"] == "raw_changed" and touched["last_modified_changed"] and not touched["body_changed"]
    edited = notice(LastModified="2026-10-12T08:00:00Z", Summary="<p>Declared notice, corrected.</p>")
    corrected = classify("who_don", old, stored("who_don", edited))
    assert corrected["kind"] == "raw_changed" and corrected["last_modified_changed"] and corrected["body_changed"]
    assert not corrected["parser_changed"]


def test_every_projected_field_is_compared():
    gdelt = {"url": "https://example.org/a", "title": "SYNTHETIC", "seendate": "20261030T010000Z"}
    for source_id, data, record_id in (("who_don", notice(), WHO_ID), ("gdelt_doc", gdelt, article_id(gdelt))):
        kept = set(stored(source_id, data, record_id)) - {"raw_content", "raw_sha256", "document_evidence"}
        assert kept == set(PROJECTED_FIELDS)


def test_route_hands_the_read_binding_to_the_store(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_READ_TOKEN", "synthetic-read-token")
    seen = {}
    monkeypatch.setattr(version_difference, "for_versions", lambda *args: seen.update(args=args) or {"versions": {}})
    app = FastAPI()
    app.include_router(history_router.router)
    version = "a" * 64
    params = {"source_id": "who_don", "is_demo": "false", "mode": "operational_replay",
              "cutoff": "2026-11-01T00:00:00Z", "version_ids": [version]}
    with TestClient(app) as client:
        assert client.get("/api/history/version-differences", params=params).status_code == 401
        answer = client.get("/api/history/version-differences", params=params,
                            headers={"Authorization": "Bearer synthetic-read-token"})
    assert answer.status_code == 200 and answer.json() == {"versions": {}}
    assert seen["args"][:4] == ("who_don", False, [version], "operational_replay")
