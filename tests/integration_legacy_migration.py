"""v3 6.3 legacy migration on a real PostgreSQL (isolated schema): mapping proven or pending, migrated versions in
their real sources with honest times, native versions never displaced, aliases on the old ids that respect the
real source's permission and the real record's state, idempotent re-runs."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import legacy_bridge
import pytest
from integration_records import _fetch_study, pg, pg_engine  # noqa: F401  -- 隔离库夹具
from sqlalchemy import text

from fetch.legacy_migration import CONVERTER, migrate, plan
from graph.publish.collected_document import CollectedDocument
from graph.publish.reviewed_graph import document_native_id
from store import records as store
from store.record_models import ArchiveImport, ArchiveRecord, FabricError, sha256

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


@pytest.fixture
def lpg(pg):
    with pg.begin() as conn:
        conn.execute(text("TRUNCATE legacy_aliases"))
    return pg


def rows(pg, sql, **params):
    with pg.begin() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def legacy(site, doc_uuid, url, markdown, key, created="2019-03-01 08:00:00"):
    doc = CollectedDocument(source_doc_uuid=doc_uuid, source_site=site, title="旧文档 " + key, source_url=url,
                            original_raw_content=markdown, original_raw_sha256=sha256(markdown),
                            original_created_at=created, state_observed_at=NOW(), capture_reason="backfill")
    native = document_native_id(doc)
    run = legacy_bridge.write(ArchiveImport(
        source_id="legacy_documents", is_demo=False, scope_description="synthetic legacy " + key, declared_count=1,
        records=[ArchiveRecord(source_record_id=native, record_kind="document", raw_content=doc.model_dump_json())]),
        "legacy-fixture:" + key)
    return native, run.version_ids[0]


def u5(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


NMPA_URL = "https://www.nmpa.gov.cn/xxgk/ggtg/ypggtg/20260522.html"
PAGE = "---\ntitle: \"国家药监局公告\"\npublish_date: 2026-05-22\nsource_site: nmpa\n---\n\n# 国家药监局公告\n\n正文第一段。"
LABEL = "---\nsource_site: fda_api_drug_label\n---\n\n# (无标题)\n\n**set_id**: abc-123\n**effective_time**: 20100104"
PROTOCOL = {"identificationModule": {"nctId": "NCT00000001", "briefTitle": "Example trial"},
            "statusModule": {"overallStatus": "RECRUITING",
                             "primaryCompletionDateStruct": {"date": "2026-06", "type": "ESTIMATED"}},
            "designModule": {"enrollmentInfo": {"count": 400, "type": "ESTIMATED"}}}


def ct_markdown(protocol, extra=""):
    return ("---\ntitle: \"Example trial\"\nsource_site: clinicaltrials\nsource_url: https://clinicaltrials.gov/api/v2/studies\n"
            "---\n\n# Example trial\n\n## 原始数据\n\n**protocolSection**: " + json.dumps(protocol) + extra)


def test_legacy_versions_go_to_their_real_sources_proven_or_pending(lpg):
    native_ct = _fetch_study("NCT00000001", "native-ct").version_ids[0]  # a native version exists for this NCT
    ct, ct_v = legacy("clinicaltrials", str(uuid.uuid4()), "https://clinicaltrials.gov/api/v2/studies",
                      ct_markdown(PROTOCOL), "ct")
    page, page_v = legacy("nmpa", u5(NMPA_URL), NMPA_URL, PAGE, "page")
    label, label_v = legacy("fda_api_drug_label", u5("fda_api_drug_label:abc-123"), "https://api.fda.gov/drug/label.json",
                            LABEL, "label")
    listed, listed_v = legacy("cde_xxgk", str(uuid.uuid4()), "https://www.cde.org.cn/main/xxgk/listpage/abc",
                              "---\nsource_site: cde_xxgk\n---\n\n表格", "list")
    broken, broken_v = legacy("clinicaltrials", str(uuid.uuid4()), "https://clinicaltrials.gov/api/v2/studies",
                              ct_markdown(PROTOCOL)[:-40], "broken")
    before = NOW()
    report = migrate(is_demo=False, apply=True)
    assert report["counts"] == {"ready": 3, "pending:detail_url_not_proven": 1, "pending:embedded_json_incomplete": 1}
    aliases = {r["legacy_version_id"]: r for r in rows(lpg, "SELECT * FROM legacy_aliases")}
    assert {v: (a["status"], a["basis"] or a["reason"]) for v, a in aliases.items()} == {
        ct_v: ("imported", "embedded_api_sections"), page_v: ("imported", "uuid5_detail_url"),
        label_v: ("imported", "uuid5_record_key"), listed_v: ("pending", "detail_url_not_proven"),
        broken_v: ("pending", "embedded_json_incomplete")}
    assert (aliases[page_v]["real_source_id"], aliases[page_v]["real_record_id"]) == ("nmpa", NMPA_URL)
    assert aliases[label_v]["real_record_id"] == u5("fda_api_drug_label:abc-123")
    assert aliases[ct_v]["real_record_id"] == "NCT00000001"
    # a legacy CT whose sections are the native version's: a content_equivalent relation, not a merge
    assert aliases[ct_v]["equivalent_version_id"] == native_ct and aliases[ct_v]["real_version_id"] != native_ct

    migrated = {r["version_id"]: r for r in rows(lpg, """
        SELECT version_id, first_seen_at, proof_available_at, payload FROM fabric_versions
        WHERE payload->'facts'->>'publication_role'='migrated_record'""")}
    assert set(migrated) == {aliases[v]["real_version_id"] for v in (ct_v, page_v, label_v)}
    for version in migrated.values():
        facts = version["payload"]["facts"]
        assert facts["legacy_migration"]["converter"] == CONVERTER
        assert facts["legacy_migration"]["claimed"]["crawl_or_created_at"] == "2019-03-01 08:00:00"  # a claim
        assert version["first_seen_at"] >= before and version["proof_available_at"] is None  # no proof made
        assert version["payload"]["availability_proof"]["kind"] == "unverified"
        assert version["payload"]["parser_version"].endswith("+" + CONVERTER)
        raw = json.loads(version["payload"]["raw_content"])
        assert raw["legacy_migration"]["legacy_raw_sha256"]  # the envelope names the immutable legacy raw
    # the legacy versions themselves are untouched
    assert len(rows(lpg, "SELECT 1 FROM fabric_versions WHERE source_id='legacy_documents'")) == 5

    # native first: the late migrated CT does not take the head; a record with only a migrated version has it
    with lpg.begin() as conn:
        now = store._now(conn)
    assert store.visible_versions("clinicaltrials", False, "current", now, ["NCT00000001"]) == {
        "NCT00000001": native_ct}
    assert store.visible_versions("nmpa", False, "current", now, [NMPA_URL]) == {
        NMPA_URL: aliases[page_v]["real_version_id"]}

    # the old id reads the same legacy version, with what it became
    read = store.read_version(ct_v, "legacy_documents", ct, False, "current", now)
    assert read.version_id == ct_v and read.raw_sha256 == rows(
        lpg, "SELECT payload->>'raw_sha256' AS h FROM fabric_versions WHERE version_id=:v", v=ct_v)[0]["h"]
    m = read.migration
    assert m["status"] == "imported" and m["target"]["version_id"] == aliases[ct_v]["real_version_id"]
    assert m["target_visible"] and m["record_current_version_id"] == native_ct
    assert not m["record_current_is_migrated"] and not m["record_withdrawn"]
    # replay before the migration: the migrated version did not exist yet
    assert store.read_version(ct_v, "legacy_documents", ct, False, "operational_replay", before).migration[
        "target_visible"] is False
    # pending keeps its reason and widens nothing
    pending = store.read_version(listed_v, "legacy_documents", listed, False, "current", now).migration
    assert pending == {"status": "pending", "converter": CONVERTER, "claimed": pending["claimed"],
                       "reason": "detail_url_not_proven"}

    # idempotent: a second run changes nothing
    count = len(rows(lpg, "SELECT 1 FROM fabric_versions"))
    again = migrate(is_demo=False, apply=True)
    assert again["counts"] == report["counts"] and len(rows(lpg, "SELECT 1 FROM fabric_versions")) == count
    assert {r["legacy_version_id"]: r["real_version_id"] for r in rows(lpg, "SELECT * FROM legacy_aliases")} == {
        v: a["real_version_id"] for v, a in aliases.items()}


def test_an_alias_follows_the_real_records_state_and_the_real_sources_permission(lpg):
    page, page_v = legacy("nmpa", u5(NMPA_URL), NMPA_URL, PAGE, "page")
    migrate(is_demo=False, apply=True)
    real = rows(lpg, "SELECT real_version_id FROM legacy_aliases WHERE legacy_version_id=:v", v=page_v)[0]
    # the real source stays readable but the real record is withdrawn (a native version saying so)
    withdrawn_raw = json.dumps({"schema_version": "datafabric.web_capture.v1", "role": "web_capture", "site": "nmpa",
                                "url": NMPA_URL, "html_sha256": "0" * 64, "html": "<html></html>",
                                "parsed": {"title": "已撤销", "markdown": "本公告已撤销。", "parse_method": "css_extract",
                                           "extra": {}}, "attachments": []}, sort_keys=True, ensure_ascii=False)
    store.import_archive(ArchiveImport(source_id="nmpa", is_demo=False, scope_description="native withdrawal",
                                       declared_count=1, records=[ArchiveRecord(
                                           source_record_id=NMPA_URL, record_kind="document",
                                           raw_content=withdrawn_raw)]), "native-withdrawn")
    with lpg.begin() as conn:  # simulated: the native version becomes the record's withdrawal notice
        conn.execute(text("ALTER TABLE fabric_versions DISABLE TRIGGER fabric_versions_append_only"))
        changed = conn.execute(text("""
            UPDATE fabric_versions SET payload=jsonb_set(payload,'{facts,publication_state}','"withdrawn"')
            WHERE source_id='nmpa' AND payload->'facts'->>'role'='web_capture'""")).rowcount
        conn.execute(text("ALTER TABLE fabric_versions ENABLE TRIGGER fabric_versions_append_only"))
        now = store._now(conn)
    assert changed == 1, rows(lpg, "SELECT source_id, payload->'facts'->>'role' AS r FROM fabric_versions")
    m = store.read_version(page_v, "legacy_documents", page, False, "current", now).migration
    assert m["target"]["version_id"] == real["real_version_id"] and m["target_visible"]
    state = rows(lpg, "SELECT payload->'facts'->>'publication_state' AS s, pg_typeof(payload)::text AS t "
                      "FROM fabric_versions WHERE version_id=:v", v=m["record_current_version_id"])
    assert m["record_withdrawn"] and not m["record_current_is_migrated"], state  # the withdrawal is not revived
    assert m["record_current_version_id"] != real["real_version_id"]
    # the real source revoked: the old id does not get round it
    with lpg.begin() as conn:
        conn.execute(text("UPDATE fabric_sources SET read_enabled=false WHERE source_id='nmpa'"))
    with pytest.raises(FabricError) as error:
        store.read_version(page_v, "legacy_documents", page, False, "current", now)
    assert error.value.status == 403


def test_plan_is_pure_and_names_every_reason():
    def payload(site, doc_uuid, url, markdown, state="active"):
        doc = {"source_site": site, "source_doc_uuid": doc_uuid, "source_url": url, "original_raw_content": markdown,
               "original_raw_sha256": sha256(markdown), "publication_state": state, "title": "t"}
        return {"source_id": "legacy_documents", "source_record_id": "legacy:x", "version_id": "a" * 64,
                "raw_sha256": "b" * 64, "raw_content": json.dumps(doc)}

    assert plan(payload("nmpa", u5(NMPA_URL), NMPA_URL, PAGE))["basis"] == "uuid5_detail_url"
    assert plan(payload("nmpa", str(uuid.uuid4()), NMPA_URL, PAGE))["reason"] == "detail_url_not_proven"
    assert plan(payload("nmpa", u5("https://example.org/x"), "https://example.org/x", PAGE))["reason"] == "url_not_of_site"
    assert plan(payload("fda_api_drug_label", str(uuid.uuid4()), "", LABEL))["reason"] == "uuid_not_reproducible"
    assert plan(payload("fda_api_drug_label", u5("x"), "", "no key"))["reason"] == "record_key_not_found"
    assert plan(payload("unknown_site", "u", "", "x"))["reason"] == "source_unknown"
    # Codex r92: ownership is proven by the parsed URL, not by a site URL carried inside another page's URL
    for foreign in ("https://example.org/fabric-review-only?next=" + NMPA_URL,
                    "https://example.org/#" + NMPA_URL,
                    "https://www.nmpa.gov.cn@example.org/x.html",
                    "https://user:pw@www.nmpa.gov.cn/xxgk/1.html",
                    "https://www.nmpa.gov.cn:8443/xxgk/1.html",
                    "ftp://www.nmpa.gov.cn/xxgk/1.html",
                    "https://nmpa.gov.cn.example.org/xxgk/1.html"):
        assert plan(payload("nmpa", u5(foreign), foreign, PAGE))["reason"] == "url_not_of_site", foreign
    chictr = "https://www.chictr.org.cn/showproj.html?proj=325180"
    assert plan(payload("chictr", u5(chictr), chictr, PAGE))["basis"] == "uuid5_detail_url"
    listing = "https://www.chictr.org.cn/searchproj.html?page=2"  # the site's own, but not a detail page
    assert plan(payload("chictr", u5(listing), listing, PAGE))["reason"] == "url_not_of_site"
    assert plan(payload("nmpa", u5(NMPA_URL), NMPA_URL, "", "withdrawn"))["reason"] == "legacy_withdrawn"
    bad = dict(PROTOCOL, identificationModule={"nctId": "NOT-AN-NCT"})
    assert plan(payload("clinicaltrials", "u", "", ct_markdown(bad)))["reason"] == "nct_not_found"
    good = plan(payload("clinicaltrials", "u", "", ct_markdown(PROTOCOL, "\n**hasResults**: False")))
    assert good["status"] == "ready" and good["envelope"]["hasResults"] is False
    assert good["block"]["scalar_rendering"] == {"hasResults": "python_str"}
    assert plan(payload("clinicaltrials", "u", "", ct_markdown(PROTOCOL, "\n**hasResults**: maybe")))[
        "reason"] == "embedded_json_incomplete"


def test_legacy_documents_is_retired_for_new_writes_and_new_queries_but_old_ids_still_read(lpg):
    """Codex r92: retirement is real - nothing new is written to legacy_documents, no new query chooses it, and an
    old id still reads its version (with its alias). Nothing is deleted."""

    page, page_v = legacy("nmpa", u5(NMPA_URL), NMPA_URL, PAGE, "page")  # written before the retirement (fixture)
    migrate(is_demo=False, apply=True)
    with lpg.begin() as conn:
        now = store._now(conn)
    with pytest.raises(FabricError) as error:
        store.import_archive(ArchiveImport(source_id="legacy_documents", is_demo=False, scope_description="new",
                                           declared_count=0, records=[]))
    assert (error.value.code, error.value.status) == ("legacy_retired", 410)
    with pytest.raises(FabricError) as error:  # a new query does not choose legacy
        store.list_records("legacy_documents", False, "current", now, terms=["公告"])
    assert error.value.code == "legacy_retired"
    by_id = store.list_records("legacy_documents", False, "current", now, source_record_ids=[page])
    assert [item.version_id for item in by_id.items] == [page_v]  # an old reference still resolves
    read = store.read_version(page_v, "legacy_documents", page, False, "current", now)
    assert read.migration["status"] == "imported"
    assert len(rows(lpg, "SELECT 1 FROM fabric_versions WHERE source_id='legacy_documents'")) == 1  # kept


def test_a_pending_legacy_version_is_blocked_when_its_original_site_is_revoked(lpg):
    """A version that never became a real-source version has no real source to re-check; the bridge's own dependency
    list is empty. Its original site is the permission: revoking it stops the old id too, restoring it restores it."""
    listed, listed_v = legacy("cde_xxgk", str(uuid.uuid4()), "https://www.cde.org.cn/main/xxgk/listpage/abc",
                              "---\nsource_site: cde_xxgk\n---\n\n表格", "pending-site")
    assert migrate(is_demo=False, apply=True)["counts"] == {"pending:detail_url_not_proven": 1}
    with lpg.begin() as conn:
        now = store._now(conn)
    assert store.read_version(listed_v, "legacy_documents", listed, False, "current", now).migration["status"] == "pending"
    with lpg.begin() as conn:
        conn.execute(text("UPDATE fabric_sources SET read_enabled=false WHERE source_id='cde_xxgk'"))
    with pytest.raises(FabricError) as error:
        store.read_version(listed_v, "legacy_documents", listed, False, "current", now)
    assert (error.value.code, error.value.status) == ("source_not_available", 403)
    with lpg.begin() as conn:
        conn.execute(text("UPDATE fabric_sources SET read_enabled=true WHERE source_id='cde_xxgk'"))
    assert store.read_version(listed_v, "legacy_documents", listed, False, "current", now).version_id == listed_v


def test_migration_never_changes_a_legacy_version_or_backfills_a_visibility_time(lpg):
    """N9: the legacy rows are byte-identical after the migration; the migrated version is visible from when it was
    adopted, never from the crawl or creation time the old system claimed."""
    page, page_v = legacy("nmpa", u5(NMPA_URL), NMPA_URL, PAGE, "page", created="2019-03-01 08:00:00")
    columns = "version_id, first_seen_at, proof_available_at, published_seq, payload"
    before = rows(lpg, f"SELECT {columns} FROM fabric_versions WHERE source_id='legacy_documents' ORDER BY version_id")
    adopted_after = NOW()
    migrate(is_demo=False, apply=True)
    migrate(is_demo=False, apply=True)
    assert rows(lpg, f"SELECT {columns} FROM fabric_versions WHERE source_id='legacy_documents' "
                     "ORDER BY version_id") == before
    (alias_row,) = rows(lpg, "SELECT real_version_id, claimed FROM legacy_aliases")
    (real,) = rows(lpg, "SELECT first_seen_at, proof_available_at FROM fabric_versions WHERE version_id=:v",
                   v=alias_row["real_version_id"].strip())
    assert real["first_seen_at"] >= adopted_after - timedelta(seconds=5)  # adopted now, not in 2019
    assert real["first_seen_at"].year >= 2026 and real["proof_available_at"] is None
    assert "2019-03-01" in json.dumps(alias_row["claimed"], default=str)  # the old claim is kept as a claim only


def test_a_new_graph_package_may_not_cite_a_retired_source_and_an_old_one_still_reads(lpg, monkeypatch):
    """N12: a package published now cites the real version a legacy one became; a package published before keeps
    validating and reading."""
    from integration_records import _publish_fixture_graph, _synthetic_graph_observation

    observation = _synthetic_graph_observation()
    old = _publish_fixture_graph(observation, "before-retirement")  # the bridge and its refusal are open (fixture)
    assert old.version_ids
    # the refusal is back in force; the writer stays open so the same documents are adopted again (idempotently)
    from graph.publish import fabric_graph

    monkeypatch.setattr(fabric_graph, "_refuse_retired_refs", legacy_bridge.REFUSE_RETIRED_REFS)
    with pytest.raises(FabricError) as error:
        _publish_fixture_graph(observation, "after-retirement")
    assert error.value.code == "graph_evidence_retired_source" and "legacy_documents" in error.value.message
    with store.fabric_engine().begin() as conn:
        stored = conn.execute(text("SELECT count(*) FROM fabric_versions WHERE source_id='reviewed_graph'")).scalar_one()
    assert stored == 1  # the old package is still there, the new one was not written
