"""资料库在真实 PostgreSQL 上（隔离 schema，不用 SQLite，不碰现役表）：版本只在内容指纹变了时新存（V042 前存的
版本也一样），按模式和截止时间读、分页、变更、上一版，登记差分，附件，GDELT 窗口。"""

import hashlib
import json
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from api.routers.fabric import router
from store import records as store
from store.migrations import runner
from store.record_models import ArchiveImport, ArchiveRecord, FabricError, FetchRequest, sha256

READ = {"Authorization": "Bearer test-reader-only"}
NMPA_PAGE = "https://www.nmpa.gov.cn/xxgk/ggtg/3.html"


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def raw_record(native, phase="PHASE1", **extra):
    data = {"record_kind": "trial", "title": native, "trial_id": native, "status": "RECRUITING", "phase": [phase],
            **extra}
    return ArchiveRecord(source_record_id=native, record_kind=data["record_kind"],
                         raw_content=json.dumps(data, ensure_ascii=False))


def archive(records, available="2016-06-01"):
    """一包导入的历史原件，声明从 available 那天起公开可见。"""
    return ArchiveImport(source_id="external_archive", is_demo=True, scope_description="explicit synthetic cohort",
                         records=records, declared_count=len(records), declared_complete=True,
                         proof={"available_at": {"value": available, "precision": "day"}})


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _chain(pg, source_id, record_id):
    """一条记录的版本按发布序号：[(version_id, replaced_by)]。"""
    with pg.begin() as conn:
        return [tuple(row) for row in conn.execute(text(
            "SELECT version_id, replaced_by FROM fabric_versions WHERE source_id=:s AND source_record_id=:r "
            "ORDER BY published_seq"), {"s": source_id, "r": record_id})]


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("DATAFABRIC_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL URL required")
    parsed = make_url(url)
    assert (
        parsed.host == "127.0.0.1"
        and (parsed.port, parsed.database) in {
            (18436, "zk_fabric_test"), (18636, "zk_fabric_identity_test"),
        }
    )
    engine = create_engine(url)
    schema = "test_records_" + uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA " + schema))
    scoped = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    yield scoped
    scoped.dispose()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA " + schema + " CASCADE"))
    engine.dispose()


@pytest.fixture
def pg(pg_engine, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_READ_TOKEN", "test-reader-only")
    monkeypatch.setenv("DATAFABRIC_ADMIN_TOKEN", "test-admin-only")
    monkeypatch.setattr(store, "fabric_engine", lambda: pg_engine)
    runner.apply("fabric", store.fabric_engine())
    with pg_engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE derived_stage_chunks,derived_stage_waiting,derived_identifiers,derived_profiles,derived_text_claims,fabric_attachment_links,derived_clean_text,derived_registry_changes,"
            "derived_registry_diff_runs,fabric_versions,fabric_runs,"
            "fabric_version_rank CASCADE"  # the index tables belong to the fabric role and reference the versions
        ))
        conn.execute(
            text("UPDATE fabric_sources SET published_seq=0,read_enabled=true")
        )
    return pg_engine


# ——一次采集读到的原件：网络读取由给定原件代替，建任务、存版本走真实入口——


def _fetch(source_id, record_id, raw, key, kind="document", query="synthetic", ids=()):
    """幂等键用过就是原来那次运行。"""
    run, fresh = store.create_fetch(FetchRequest(source_id=source_id, query=query, source_record_ids=list(ids)), key)
    if not fresh:
        return run
    store.job_input(run.id, run.attempt)
    return store.finish_fetch(run.id, run.attempt, {
        "records": [ArchiveRecord(source_record_id=record_id, record_kind=kind, raw_content=raw)],
        "complete": False, "upstream_pagination_exhausted": True, "stop_reason": "end", "requests": 1,
        "bytes": len(raw.encode()), "source_total": 1, "error": None,
    })


def _epmc(abstract="A", native="123"):
    return json.dumps({"source": "MED", "id": native, "title": "Example", "abstractText": abstract})


def _fetch_europe_pmc_raw(raw, key):
    return _fetch("europe_pmc", "MED:123", raw, key)


def _study(native, **status):
    module = {"overallStatus": status.get("status", "RECRUITING")}
    if status.get("why"):
        module["whyStopped"] = status["why"]
    module["primaryCompletionDateStruct"] = {"date": status.get("pcd", "2026-06"), "type": status.get("pcd_type", "ESTIMATED")}
    return json.dumps({
        "protocolSection": {
            "identificationModule": {"nctId": native, "briefTitle": "Example trial"},
            "statusModule": module,
            "designModule": {"enrollmentInfo": {"count": status.get("n", 400), "type": "ESTIMATED"}},
            **({"descriptionModule": {"briefSummary": status["summary"]}} if status.get("summary") else {}),
        },
    }, sort_keys=True)


def _fetch_study(native, key, **status):
    return _fetch("clinicaltrials", native, _study(native, **status), key, "trial")


def _trial(holder, status="RECRUITING"):
    """ClinicalTrials 返回的一条登记，带着接口当天的数据日期（versionHolder）。"""
    return json.dumps({
        "protocolSection": {"identificationModule": {"nctId": "NCT00000123", "briefTitle": "Example trial"},
                            "statusModule": {"overallStatus": status}},
        "derivedSection": {"miscInfoModule": {"versionHolder": holder}},
    })


def _web_page(html, markdown="# 公告"):
    """站点插件存下的一张 NMPA 详情页：HTML、它的哈希和解析结果。"""
    from fetch.site_crawler import WEB_SCHEMA

    return json.dumps({"schema_version": WEB_SCHEMA, "role": "web_capture", "site": "nmpa", "url": NMPA_PAGE,
                       "fetched_at": "t", "html_sha256": hashlib.sha256(html.encode()).hexdigest(), "html": html,
                       "parsed": {"title": "公告", "publish_date": "2026-11-01", "markdown": markdown,
                                  "parse_method": "css", "extra": {}},
                       "attachments": []}, sort_keys=True)


# ——版本：内容指纹 fingerprint(source_id, raw) 相同就是没变——


def test_a_version_is_new_only_when_the_content_changes(pg):
    """The same original again, or in another key order (Europe PMC, live 2026-11-03: 23927 versions for 8444
    records), is the stored version; a changed body is a new version the old one's replaced_by points to; A -> B -> A
    is a third version."""
    record = {"source": "MED", "id": "123", "title": "Example", "abstractText": "A"}
    a = _fetch_europe_pmc_raw(json.dumps(record), "fp-a")
    same = [_fetch_europe_pmc_raw(json.dumps(record), "fp-same"),
            _fetch_europe_pmc_raw(json.dumps(dict(reversed(list(record.items()))), indent=1), "fp-order")]
    assert [run.coverage["new_version_count"] for run in same] == [0, 0]
    assert all(run.version_ids == a.version_ids for run in same)
    b = _fetch_europe_pmc_raw(_epmc("B"), "fp-b")
    back = _fetch_europe_pmc_raw(_epmc("A"), "fp-back")
    versions = [run.version_ids[0] for run in (a, b, back)]
    assert _chain(pg, "europe_pmc", "MED:123") == [(versions[0], versions[1]), (versions[1], versions[2]),
                                                   (versions[2], None)]
    current = store.list_records("europe_pmc", False, "current", datetime.now().astimezone())
    assert [item.version_id for item in current.items] == [versions[2]]


def test_a_new_data_date_or_new_html_alone_is_not_a_new_version(pg):
    """ClinicalTrials stamps every study with the day's data date; NMPA and ChiCTR pages come with new generated ids in
    their HTML (live 2026-11-11: a new version on every fetch). Neither changes the record; a new status or text does."""
    trial = _fetch("clinicaltrials", "NCT00000123", _trial("2026-11-02"), "holder-1", "trial")
    assert _fetch("clinicaltrials", "NCT00000123", _trial("2026-11-03"), "holder-2", "trial").version_ids == \
        trial.version_ids
    page = _fetch("nmpa", NMPA_PAGE, _web_page("<a id='aria1'>"), "html-1")
    assert _fetch("nmpa", NMPA_PAGE, _web_page("<a id='aria2'>"), "html-2").version_ids == page.version_ids
    with pg.begin() as conn:  # the originals kept are the first ones seen
        kept = conn.execute(text(
            "SELECT payload->>'raw_content' FROM fabric_versions ORDER BY source_id")).scalars().all()
    assert len(kept) == 2 and '"2026-11-02"' in kept[0] and "aria1" in kept[1]
    status = _fetch("clinicaltrials", "NCT00000123", _trial("2026-11-04", "COMPLETED"), "holder-3", "trial")
    corrected = _fetch("nmpa", NMPA_PAGE, _web_page("<a id='aria3'>", "# 公告（更正）"), "html-3")
    assert status.coverage["new_version_count"] == corrected.coverage["new_version_count"] == 1


def test_stored_unchanged_answers_what_storing_would_do(pg):
    """The incremental read stops after three stored records in a row (Budget.known): it asks stored_unchanged, which
    has to agree with what storing the original would do."""
    def item(raw, record_id="MED:123"):
        return ArchiveRecord(source_record_id=record_id, record_kind="document", raw_content=raw)

    record = {"source": "MED", "id": "123", "title": "Example", "abstractText": "A"}
    assert not store.stored_unchanged("europe_pmc", item(json.dumps(record)))  # not stored yet
    _fetch_europe_pmc_raw(json.dumps(record), "known-a")
    assert store.stored_unchanged("europe_pmc", item(json.dumps(dict(reversed(list(record.items()))))))
    assert not store.stored_unchanged("europe_pmc", item(_epmc("B")))
    assert _fetch_europe_pmc_raw(_epmc("B"), "known-b").coverage["new_version_count"] == 1
    assert store.stored_unchanged("europe_pmc", item(_epmc("B")))
    _fetch("clinicaltrials", "NCT00000123", _trial("2026-11-02"), "known-trial", "trial")
    assert store.stored_unchanged("clinicaltrials", item(_trial("2026-11-05"), "NCT00000123"))


def _old_version(conn, native, abstract, seq):
    """V042 以前存下的一个 Europe PMC 版本：没有 fingerprint、replaced_by，表还是旧名 history_versions。"""
    from fetch.projection import project_record
    from store.record_models import AvailabilityProof, VersionRecord

    record_id, raw = "MED:" + native, _epmc(abstract, native)
    now = store._now(conn)
    version_id = sha256(f"before-v042|{record_id}|{seq}")
    payload = VersionRecord(
        source_id="europe_pmc", source_record_id=record_id, version_id=version_id, is_demo=False, raw_content=raw,
        raw_sha256=sha256(raw), first_seen_at=now, received_at=now, availability_proof=AvailabilityProof(),
        published_seq=seq, **project_record("europe_pmc", ArchiveRecord(
            source_record_id=record_id, record_kind="document", raw_content=raw)))
    conn.execute(text(
        "INSERT INTO history_versions (version_id,source_id,is_demo,source_record_id,record_kind,first_seen_at,"
        "received_at,published_seq,payload) VALUES (:id,'europe_pmc',false,:record,'document',:now,:now,:seq,"
        "CAST(:payload AS jsonb))"),
        {"id": version_id, "record": record_id, "now": now, "seq": seq, "payload": payload.model_dump_json()})


def test_v042_on_versions_stored_before_it(pg_engine, tmp_path, monkeypatch):
    """V042 on a database that has versions: replaced_by links each record's versions by published_seq; afterwards a
    version row takes only fingerprint and replaced_by; an old current version without a fingerprint is compared on
    the spot; ops/backfill_fingerprint.py fills every fingerprint in. The database is built as it was before V042,
    with the old table names (history_*): V046 renames them with the data in place."""
    from ops import backfill_fingerprint

    schema = "test_v042_" + uuid.uuid4().hex
    with pg_engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA " + schema))
    engine = create_engine(pg_engine.url, connect_args={"options": "-csearch_path=" + schema})
    later = sorted(p for p in runner.VERSIONS.glob("V*.sql") if runner.version(p) >= "V042")
    monkeypatch.setattr(runner, "VERSIONS", shutil.copytree(runner.VERSIONS, tmp_path / "versions"))
    for path in later:
        (runner.VERSIONS / path.name).unlink()
    monkeypatch.setattr(store, "fabric_engine", lambda: engine)
    seed = runner.seed_sources
    monkeypatch.setattr(runner, "seed_sources", lambda engine: None)  # it fills fabric_sources, named by V046
    try:
        runner.apply("fabric", engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO history_sources (source_id, catalog) VALUES ('europe_pmc', '{}')"))
            for seq, (native, abstract) in enumerate([("1", "A"), ("1", "B"), ("2", "A"), ("1", "C")], 1):
                _old_version(conn, native, abstract, seq)
            conn.execute(text("UPDATE history_sources SET published_seq=4 WHERE source_id='europe_pmc'"))
        for path in later:
            shutil.copy(path, runner.VERSIONS)
        monkeypatch.setattr(runner, "seed_sources", seed)
        assert runner.apply("fabric", engine) == [p.name for p in later]
        one, two = _chain(engine, "europe_pmc", "MED:1"), _chain(engine, "europe_pmc", "MED:2")
        assert [replaced for _, replaced in one] == [one[1][0], one[2][0], None] and two[0][1] is None
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
            conn.execute(text("UPDATE fabric_versions SET payload=jsonb_set(payload,'{title}','\"changed\"')"))
        assert _fetch("europe_pmc", "MED:1", _epmc("C", "1"), "after-v042").coverage["new_version_count"] == 0
        assert backfill_fingerprint.backfill(3) == 4
        with engine.begin() as conn:
            stored = conn.execute(text(
                "SELECT fingerprint, source_id, payload->>'raw_content' FROM fabric_versions")).all()
        assert len(stored) == 4 and all(fp == store.fingerprint(source, raw) for fp, source, raw in stored)
    finally:
        engine.dispose()
        with pg_engine.begin() as conn:
            conn.execute(text("DROP SCHEMA " + schema + " CASCADE"))


def test_version_differences_name_the_previous_version(pg):
    v1 = _fetch_study("NCT09990003", "vd-1").version_ids[0]
    v2 = _fetch_study("NCT09990003", "vd-2", n=200).version_ids[0]
    with pg.begin() as conn:
        first_seen = conn.execute(
            text("SELECT first_seen_at FROM fabric_versions WHERE version_id=:v"), {"v": v1}).scalar_one()
    client = _client()

    def differences(cutoff, *version_ids):
        response = client.get("/api/fabric/version-differences", headers=READ, params={
            "source_id": "clinicaltrials", "is_demo": "false", "mode": "operational_replay",
            "cutoff": cutoff.isoformat(), "version_ids": list(version_ids)})
        assert response.status_code == 200, response.text
        return response.json()["versions"]

    unknown = "f" * 64
    assert differences(datetime.now(timezone.utc), v1, v2, unknown) == {
        v1: {"state": "first_adoption", "source_record_id": "NCT09990003"},
        v2: {"state": "compared", "source_record_id": "NCT09990003", "from_version_id": v1, "kind": "raw_changed"},
        unknown: {"state": "not_visible"}}
    assert differences(first_seen, v2) == {v2: {"state": "not_visible"}}  # v2 was not stored yet


def test_a_migrated_legacy_version_keeps_its_own_chain(pg):
    """A legacy version migrated into its real source (fetch/legacy_migration.py) neither replaces the source's own
    current version nor becomes what the next fetch is compared with."""
    from fetch.legacy_migration import CONVERTER

    native = _fetch_study("NCT09990004", "role-native").version_ids[0]
    envelope = dict(json.loads(_study("NCT09990004")),
                    legacy_migration={"converter": CONVERTER, "legacy_version_id": "a" * 64})
    migrated = store.import_archive(ArchiveImport(
        source_id="clinicaltrials", is_demo=False, scope_description="synthetic legacy migration", declared_count=1,
        records=[ArchiveRecord(source_record_id="NCT09990004", record_kind="trial", raw_content=json.dumps(envelope))]),
        "role-migrated").version_ids[0]
    assert _fetch_study("NCT09990004", "role-native-again").coverage["new_version_count"] == 0
    assert _chain(pg, "clinicaltrials", "NCT09990004") == [(native, None), (migrated, None)]
    now = datetime.now(timezone.utc)
    assert store.visible_versions("clinicaltrials", False, "current", now, ["NCT09990004"]) == {"NCT09990004": native}


def test_two_fetches_of_the_same_change_at_once_store_it_once(pg):
    first = _fetch_europe_pmc_raw(_epmc("A"), "concurrent-first")
    _fetch_europe_pmc_raw(_epmc("B"), "concurrent-changed")
    with ThreadPoolExecutor(max_workers=2) as pool:
        restored = list(pool.map(lambda index: _fetch_europe_pmc_raw(_epmc("A"), f"concurrent-restore-{index}"), [1, 2]))
    assert restored[0].version_ids == restored[1].version_ids != first.version_ids
    assert sorted(run.coverage["new_version_count"] for run in restored) == [0, 1]
    assert store.list_changes("europe_pmc", False)["max_seq"] == 3


# ——运行、读取、分页、变更——


def test_an_import_key_returns_its_run_and_reads_follow_mode_and_cutoff(pg):
    request = archive([raw_record("T1")])
    first = store.import_archive(request, "archive-first")
    assert store.import_archive(archive([raw_record("T2")]), "archive-first").id == first.id  # the key's run
    assert store.import_archive(request).version_ids == first.version_ids  # no key: a new run, the same version
    old = store.read_version(first.version_ids[0], "external_archive", "T1", True, "public_history_reconstruction",
                             date("2016-12-31T23:59:59Z"))
    assert old.first_seen_at.year >= 2026  # visible from the declared time, first seen now
    with pytest.raises(FabricError, match="不可见"):
        store.read_version(old.version_id, "external_archive", "T1", True, "operational_replay",
                           date("2016-12-31T23:59:59Z"))
    fetch, fresh = store.create_fetch(FetchRequest(source_id="clinicaltrials", query="synthetic"), "fetch-key")
    again, fresh_again = store.create_fetch(FetchRequest(source_id="clinicaltrials", query="other"), "fetch-key")
    assert (fresh, fresh_again, again.id, fetch.is_demo) == (True, False, fetch.id, False)


def test_eligible_before_latest_and_late_old_archive_does_not_replace_new(pg):
    later = store.import_archive(archive([raw_record("T1", "PHASE2")], "2018-06-01"), "later-first")
    earlier = store.import_archive(archive([raw_record("T1", "PHASE1")], "2016-06-01"), "earlier-backfill")
    old = store.list_records("external_archive", True, "public_history_reconstruction", date("2017-01-01T00:00:00Z"))
    new = store.list_records("external_archive", True, "public_history_reconstruction", date("2019-01-01T00:00:00Z"))
    assert old.items[0].version_id == earlier.version_ids[0]
    assert new.items[0].version_id == later.version_ids[0]


def test_with_the_same_visibility_time_the_later_source_revision_is_read(pg):
    newer = store.import_archive(archive([raw_record("T2", "PHASE2", source_updated_at="2015-06-01")]), "newer")
    older = store.import_archive(archive([raw_record("T2", "PHASE1", source_updated_at="2015-01-01")]), "older")
    cutoff = date("2017-01-01T00:00:00Z")
    selected = store.list_records("external_archive", True, "public_history_reconstruction", cutoff,
                                  source_record_ids=["T2"])
    assert selected.items[0].version_id == newer.version_ids[0]
    preserved = store.read_version(older.version_ids[0], "external_archive", "T2", True,
                                   "public_history_reconstruction", cutoff)
    assert preserved.facts["phase"] == ["PHASE1"]


def test_a_page_cursor_keeps_its_published_bound_and_a_disabled_source_is_not_read(pg):
    initial = store.import_archive(archive([raw_record("A"), raw_record("B"), raw_record("C")]), "initial")
    cutoff = date("2020-01-01T00:00:00Z")
    first = store.list_records("external_archive", True, "public_history_reconstruction", cutoff,
                               run_id=initial.id, limit=1)
    assert first.next_cursor == "3|A" and first.coverage == {"max_seq": 3, "has_more": True}
    store.import_archive(archive([raw_record("B", "PHASE2")], "2018-01-01"), "B-update")
    second = store.list_records("external_archive", True, "public_history_reconstruction", cutoff,
                                run_id=initial.id, limit=1, cursor=first.next_cursor)
    assert second.items[0].facts["phase"] == ["PHASE1"]  # B's new version came after the first page
    with pytest.raises(FabricError) as error:
        store.list_records("external_archive", True, "current", cutoff, cursor="not-a-cursor")
    assert error.value.code == "invalid_cursor"
    with pg.begin() as conn:
        conn.execute(text("UPDATE fabric_sources SET read_enabled=false WHERE source_id='external_archive'"))
    for read in (lambda: store.read_run(initial.id), lambda: store.read_version(
            first.items[0].version_id, "external_archive", "A", True, "public_history_reconstruction", cutoff)):
        with pytest.raises(FabricError) as error:
            read()
        assert (error.value.code, error.value.status) == ("source_not_available", 403)


def test_query_literal_and_latest_not_old_matching_version(pg):
    store.import_archive(archive([raw_record("T1", summary="old matchedword")], "2016-01-01"), "old-word")
    store.import_archive(archive([raw_record("T1", summary="new content")], "2018-01-01"), "new-word")
    cutoff = date("2019-01-01T00:00:00Z")
    assert store.list_records("external_archive", True, "public_history_reconstruction", cutoff,
                              terms=["matchedword"]).items == []
    assert store.list_records("external_archive", True, "public_history_reconstruction", cutoff,
                              terms=["%_"]).items == []


def test_changes_are_read_after_a_published_seq(pg):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda native: store.import_archive(archive([raw_record(native)]), "concurrent-" + native),
                      ["A", "B"]))
    first = store.list_changes("external_archive", True, limit=1)
    assert (len(first["items"]), first["next_after"], first["max_seq"]) == (1, 1, 2)
    store.import_archive(archive([raw_record("C")]), "after-first-page")
    rest = store.list_changes("external_archive", True, after=first["next_after"])
    assert [item["published_seq"] for item in rest["items"]] == [2, 3] and rest["next_after"] == 3
    assert rest["items"][-1]["source_record_id"] == "C"


def test_two_imports_of_one_source_that_both_registered_their_run_publish_one_after_the_other(pg, monkeypatch):
    """Two archive imports of one source, each past its run insert (which takes FOR KEY SHARE on the source row
    through the foreign key) before either publishes: publishing locks the row against the other publisher only,
    so both complete in turn - no deadlock (the interleaving is forced by a barrier, not by timing)."""
    import threading

    barrier = threading.Barrier(2, timeout=30)
    publish = store._publish

    def both_registered_first(*args, **kwargs):
        barrier.wait()  # each transaction holds its run insert's key-share lock here
        return publish(*args, **kwargs)

    monkeypatch.setattr(store, "_publish", both_registered_first)

    def submit(native):
        return store.import_archive(archive([raw_record(native)]), "barrier-" + native)

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(submit, ["A", "B"]))
    assert [r.state for r in runs] == ["completed", "completed"]
    with pg.begin() as conn:
        seqs = sorted(conn.execute(text(
            "SELECT published_seq FROM fabric_versions WHERE source_id='external_archive'")).scalars())
    assert seqs == [1, 2]


def test_a_finished_or_other_attempt_cannot_store_versions(pg):
    run, _ = store.create_fetch(FetchRequest(source_id="europe_pmc", query="obesity"), "fetch-cas")
    batch = {"records": [], "complete": False, "upstream_pagination_exhausted": True, "stop_reason": "upstream_no_match",
             "requests": 1, "bytes": 10, "source_total": 0, "error": None}
    with pytest.raises(FabricError, match="旧任务"):
        store.finish_fetch(run.id, 1, batch)  # not started
    store.job_input(run.id, 1)
    with pytest.raises(FabricError, match="旧任务"):
        store.finish_fetch(run.id, 2, batch)
    assert store.finish_fetch(run.id, 1, batch).state == "completed"
    with pytest.raises(FabricError, match="旧任务"):
        store.finish_fetch(run.id, 1, batch)


def test_http_auth_and_the_raw_original(pg):
    client = _client()
    assert client.get("/api/fabric/sources").status_code == 401
    admin = {"Authorization": "Bearer test-admin-only", "Idempotency-Key": "http-archive"}
    body = archive([raw_record("A")]).model_dump(mode="json")
    assert client.post("/api/fabric/archives", json=body, headers=READ).status_code == 403
    result = client.post("/api/fabric/archives", json=body, headers=admin)
    assert result.status_code == 200, result.text
    params = {"source_id": "external_archive", "source_record_id": "A", "is_demo": "true",
              "mode": "public_history_reconstruction", "cutoff": "2017-01-01T00:00:00Z"}
    uri = "/api/fabric/versions/" + result.json()["version_ids"][0]
    record = client.get(uri, params=params, headers=READ)
    assert record.status_code == 200
    raw = client.get(uri + "/raw", params=params, headers=READ)
    assert raw.text == body["records"][0]["raw_content"]
    assert raw.headers["x-content-sha256"] == record.json()["raw_sha256"]
    assert client.get(uri, params={**params, "is_demo": "false"}, headers=READ).status_code == 404
    assert client.get(uri, params={**params, "cutoff": "2035-01-01T00:00:00Z"}, headers=READ).status_code == 422
    with pg.begin() as conn:
        conn.execute(text("UPDATE fabric_sources SET read_enabled=false WHERE source_id='external_archive'"))
    assert client.get(uri + "/raw", params=params, headers=READ).status_code == 403


def test_ranking_keys_are_derived_with_each_version_and_a_partial_backfill_changes_nothing(pg):
    """Live 2026-11-03: listing Europe PMC decompressed every payload (2s a pass) and timed out. V021 keeps
    each version's ranking keys in fabric_version_rank; a version without them is ranked from its payload,
    same result. fabric_versions itself takes no UPDATE."""
    def ranks():
        with pg.begin() as conn:
            return conn.execute(text(
                "SELECT h.source_record_id, r.publication_role FROM fabric_versions h "
                "LEFT JOIN fabric_version_rank r ON r.version_id=h.version_id ORDER BY h.published_seq")).all()

    for abstract, key in (("A", "rank-1"), ("B", "rank-2")):
        _fetch_europe_pmc_raw(_epmc(abstract), key)
    assert [r[1] for r in ranks()] == ["source_record", "source_record"]  # written at publish
    now = datetime.now().astimezone()
    derived = store.list_records("europe_pmc", False, "current", now)
    with pg.begin() as conn:  # as before V021 was installed
        conn.execute(text("TRUNCATE fabric_version_rank"))
    raw = store.list_records("europe_pmc", False, "current", now)
    assert [i.version_id for i in raw.items] == [i.version_id for i in derived.items]
    assert store.backfill_rank("europe_pmc", batch=1) == 1  # one derived, one still ranked from its payload
    half = store.list_records("europe_pmc", False, "current", now)
    assert [i.version_id for i in half.items] == [i.version_id for i in derived.items]
    assert store.backfill_rank("europe_pmc", batch=10) == 1 and store.backfill_rank("europe_pmc") == 0
    assert [r[1] for r in ranks()] == ["source_record", "source_record"]
    with pg.begin() as conn:  # derived keys are as immutable as the versions they come from
        conn.execute(text("UPDATE fabric_version_rank SET publication_role='context_reference'"))
    assert [r[1] for r in ranks()] == ["source_record", "source_record"]


def test_migration_leaves_a_seeded_row_alone_and_the_list_shows_the_declared_name(pg):
    from fetch.sources import SOURCE_CATALOG

    with pg.begin() as conn:
        conn.execute(text("UPDATE fabric_sources SET catalog=catalog || CAST(:m AS jsonb) WHERE source_id='nmpa'"),
                     {"m": json.dumps({"name": "旧说明"})})
    runner.apply("fabric", store.fabric_engine())
    with pg.begin() as conn:
        catalog = conn.execute(text("SELECT catalog FROM fabric_sources WHERE source_id='nmpa'")).scalar_one()
    assert catalog["name"] == "旧说明"  # the row is the snapshot taken when it was seeded
    listed = {row["source_id"]: row for row in store.sources_receipt()["sources"]}["nmpa"]
    assert listed["name"] == SOURCE_CATALOG["nmpa"]["name"]


def test_document_full_text_correction_survives_real_storage_and_old_version_read(pg):
    original = ArchiveRecord(
        source_record_id="SYNTHETIC-order-original", record_kind="document",
        raw_content=json.dumps({"record_kind":"document","title":"原始公告",
            "summary":"摘要没有金额","full_text":"完整原文：40套、8000万元。",
            "material_kind":"corporate_disclosure"}, ensure_ascii=False))
    old_run = store.import_archive(archive([original]), "fulltext-original")
    old = store.read_version(old_run.version_ids[0], "external_archive",
                             original.source_record_id, True, "current",
                             datetime.now().astimezone())
    correction = ArchiveRecord(
        source_record_id="SYNTHETIC-order-correction", record_kind="document",
        raw_content=json.dumps({"record_kind":"document","title":"更正公告",
            "full_text":"前述公告更正为32套、6400万元。", "material_kind":"corporate_disclosure",
            "document_relations":[{"relation_type":"corrects","target_source_id":"external_archive",
                "target_source_record_id":original.source_record_id,"target_version_id":old.version_id}]},
            ensure_ascii=False))
    new_run = store.import_archive(archive([correction], available="2016-07-01"), "fulltext-correction")
    repeated = store.import_archive(archive([correction], available="2016-07-01"), "fulltext-correction")
    assert repeated.id == new_run.id
    new = store.read_version(new_run.version_ids[0], "external_archive",
                             correction.source_record_id, True, "current",
                             datetime.now().astimezone())
    assert new.clean_text.endswith("32套、6400万元。")
    relation = new.document_evidence["document_relations"][0]
    assert relation["target_version_id"] == old.version_id
    assert relation["basis_locator"]["json_pointer"] == "/document_relations/0"
    again = store.read_version(old.version_id, "external_archive", original.source_record_id,
                               True, "current", datetime.now().astimezone())
    assert again.raw_content == old.raw_content == original.raw_content
    assert again.clean_text.endswith("40套、8000万元。")


# ——登记差分（V022）：清洗时和上一版比着写；读取遵守模式和截止时间——


def test_registry_diff_written_when_cleaned_and_read_with_visibility(pg):
    from clean import clean_stage
    from clean.registry_changes import for_versions

    first = _fetch_study("NCT09990001", "rd-1")
    same = _fetch_study("NCT09990001", "rd-2")  # 同内容不产生新版本，也不产生差分
    assert same.version_ids == first.version_ids
    second = _fetch_study("NCT09990001", "rd-3", status="TERMINATED", why="Futility", pcd="2027-06")
    v1, v2 = first.version_ids[0], second.version_ids[0]
    assert v1 != v2
    assert for_versions("clinicaltrials", False, [v2], "current", datetime.now(timezone.utc))["versions"][v2] == {
        "state": "not_processed"}  # 还没清洗
    clean_stage.process(batch=50)
    with pg.begin() as conn:
        runs = conn.execute(text(
            "SELECT to_version_id,from_version_id,pairing,change_count,material_count "
            "FROM derived_registry_diff_runs ORDER BY created_at"
        )).all()
        assert [(r[0], r[1], r[2]) for r in runs] == [(v1, None, "current_at_adoption"), (v2, v1, "current_at_adoption")]
        # 只追加
        assert conn.execute(text("DELETE FROM derived_registry_changes")).rowcount == 0
    now = datetime.now(timezone.utc)
    got = for_versions("clinicaltrials", False, [v1, v2], "current", now)["versions"]
    assert got[v1]["state"] == "first_adoption" and got[v1]["changes"] == []
    changes = {c["field"]: c for c in got[v2]["changes"]}
    assert got[v2]["state"] == "processed" and got[v2]["from_version_id"] == v1
    assert changes["status"]["new_value"] == {"present": True, "value": "TERMINATED"}
    assert changes["status"]["json_pointer"] == "/protocolSection/statusModule/overallStatus"
    assert changes["why_stopped"]["old_value"] == {"present": False}
    assert changes["primary_completion_date"]["detail"]["direction"] == "later"
    assert all(c["material"] for c in changes.values())
    # 两版都在采用之前的截止时间：看不到
    before = for_versions("clinicaltrials", False, [v2], "current", date("2020-01-01T00:00:00Z"))
    assert before["versions"][v2] == {"state": "not_processed"}
    # 公开历史重建模式：观察版本没有档案证明，不可见
    hidden = for_versions("clinicaltrials", False, [v2], "public_history_reconstruction", now)
    assert hidden["versions"][v2] == {"state": "not_processed"}
    with pytest.raises(FabricError):
        for_versions("clinicaltrials", False, [v2], "current", now + timedelta(days=1))


def test_registry_diff_backfill_pairs_existing_versions_once(pg):
    from clean.registry_changes import backfill, for_versions

    a = _fetch_study("NCT09990002", "bf-1").version_ids[0]
    b = _fetch_study("NCT09990002", "bf-2", n=200).version_ids[0]
    with pg.begin() as conn:  # 模拟改造前已有的存量：没有差分行
        conn.execute(text("ALTER TABLE derived_registry_changes DISABLE RULE derived_registry_changes_no_delete"))
        conn.execute(text("ALTER TABLE derived_registry_diff_runs DISABLE RULE derived_registry_diff_runs_no_delete"))
        conn.execute(text("DELETE FROM derived_registry_changes"))
        conn.execute(text("DELETE FROM derived_registry_diff_runs"))
        conn.execute(text("ALTER TABLE derived_registry_changes ENABLE RULE derived_registry_changes_no_delete"))
        conn.execute(text("ALTER TABLE derived_registry_diff_runs ENABLE RULE derived_registry_diff_runs_no_delete"))
    assert for_versions("clinicaltrials", False, [b], "current", datetime.now(timezone.utc))["versions"][b] == {
        "state": "not_processed"
    }
    assert backfill("clinicaltrials", batch=1) == {"processed": 2, "skipped_archive_mix": 0}
    assert backfill("clinicaltrials") == {"processed": 0, "skipped_archive_mix": 0}
    got = for_versions("clinicaltrials", False, [a, b], "current", datetime.now(timezone.utc))["versions"]
    assert got[a]["state"] == "first_adoption" and got[b]["pairing"] == "backfill_published_seq"
    enrollment = {c["field"]: c for c in got[b]["changes"]}["enrollment"]
    assert enrollment["detail"]["delta"] == -200 and enrollment["material"]


# ——附件（V023）：旧站点插件的附件版本 → 加工层 attachment_parse → 派生正文——


def _fetch_attachment(tmp_path, key, content=b"%PDF-1.4 synthetic notice"):
    from fetch.site_crawler import attachment_record_id

    path = tmp_path / (key + ".pdf")
    path.write_bytes(content)
    url = "https://www.nmpa.gov.cn/directory/web/nmpa/images/" + key + ".pdf"
    parent = "https://www.nmpa.gov.cn/xxgk/ggtg/1.html"
    raw = json.dumps({
        "schema_version": "datafabric.attachment.v1", "role": "attachment", "site": "nmpa", "url": url,
        "parent_record_id": parent, "file_name": "附件1", "file_ext": ".pdf",
        "sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "store_path": str(path),
        "discovered_from": "legacy_attachments_table", "status": "stored",
    }, ensure_ascii=False, sort_keys=True)
    done = _fetch("nmpa", attachment_record_id(parent, url), raw, key, query="legacy_attachments", ids=[parent])
    return done.version_ids[0], path


def test_attachment_parsed_once_with_retry_on_failure_and_read_with_visibility(pg, tmp_path, monkeypatch):
    from clean import attachment_parse as ap
    from clean import derived_text

    version, path = _fetch_attachment(tmp_path, "att-1")
    answers = iter([{"success": False, "markdown": "", "error": "timeout"},
                    {"success": True, "markdown": "# 附件\n\n药品注册证书信息。", "error": ""}])
    monkeypatch.setattr(ap, "_mineru", lambda p: next(answers))
    assert derived_text.process(batch=10, retry_after_seconds=0) == {"parsed": 0, "failed": 1, "unsupported": 0}
    now = datetime.now(timezone.utc)
    got = derived_text.for_versions("nmpa", False, [version], "current", now)["versions"][version]
    assert got["status"] == "failed" and got["error"] == "parser_failed" and got["attempt"] == 1
    assert derived_text.process(batch=10, retry_after_seconds=0) == {"parsed": 1, "failed": 0, "unsupported": 0}
    assert derived_text.process(batch=10, retry_after_seconds=0) == {"parsed": 0, "failed": 0, "unsupported": 0}
    got = derived_text.for_versions("nmpa", False, [version], "current", now + timedelta(seconds=0))["versions"][version]
    assert got["status"] == "parsed" and got["attempt"] == 2 and got["text"].endswith("药品注册证书信息。")
    assert got["parse_method"] == "mineru" and got["processor_version"] == ap.PROCESSOR_VERSION
    hidden = derived_text.for_versions("nmpa", False, [version], "current", date("2020-01-01T00:00:00Z"))
    assert hidden["versions"][version] == {"state": "not_visible_or_not_processed"}
    with pg.begin() as conn:
        assert conn.execute(text("DELETE FROM derived_clean_text")).rowcount == 0


def test_attachment_failures_stop_after_the_attempt_limit(pg, tmp_path, monkeypatch):
    from clean import attachment_parse as ap
    from clean import derived_text

    version, path = _fetch_attachment(tmp_path, "att-2")
    monkeypatch.setattr(ap, "_mineru", lambda p: {"success": False, "markdown": "", "error": "x"})
    for _ in range(derived_text.MAX_ATTEMPTS + 2):
        derived_text.process(batch=10, retry_after_seconds=0)
    with pg.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM derived_clean_text WHERE version_id=:v"),
                            {"v": version}).scalar_one() == derived_text.MAX_ATTEMPTS
    # 原件被改动：摘要不符，直接记失败、不调解析服务
    version3, path3 = _fetch_attachment(tmp_path, "att-3")
    path3.write_bytes(b"tampered")
    monkeypatch.setattr(ap, "_mineru", lambda p: pytest.fail("must not parse a tampered original"))
    derived_text.process(batch=10, retry_after_seconds=0)
    got = derived_text.for_versions("nmpa", False, [version3], "current", datetime.now(timezone.utc))
    assert got["versions"][version3]["error"] == "original_digest_mismatch"


def test_same_file_twice_is_parsed_once(pg, tmp_path, monkeypatch):
    from clean import attachment_parse as ap
    from clean import derived_text

    calls = []
    monkeypatch.setattr(ap, "_mineru", lambda p: calls.append(p) or {"success": True, "markdown": "# 同一份", "error": ""})
    first, _ = _fetch_attachment(tmp_path, "same-a", content=b"%PDF-1.4 identical")
    second, _ = _fetch_attachment(tmp_path, "same-b", content=b"%PDF-1.4 identical")
    derived_text.process(batch=10, retry_after_seconds=0)
    derived_text.process(batch=10, retry_after_seconds=0)
    got = derived_text.for_versions("nmpa", False, [first, second], "current", datetime.now(timezone.utc))["versions"]
    assert len(calls) == 1 and got[first]["text"] == got[second]["text"] == "# 同一份"
    assert {got[first]["parse_method"], got[second]["parse_method"]} == {"mineru", "reused_same_sha256:mineru"}


def test_an_attachment_being_parsed_is_skipped_by_a_second_worker(pg, tmp_path, monkeypatch):
    """Codex r59：两个实例不能同时解析同一版本。解析期间那一行锁着，另一个进程领活时跳过它。"""
    from clean import attachment_parse as ap
    from clean import derived_text

    _fetch_attachment(tmp_path, "claim-1")
    second = []

    def slow_parse(p):  # 第一个进程还在解析：第二个进程来领活
        second.append(derived_text.process(batch=5, retry_after_seconds=0))
        return {"success": True, "markdown": "# x", "error": ""}

    monkeypatch.setattr(ap, "_mineru", slow_parse)
    assert derived_text.process(batch=5, retry_after_seconds=0)["parsed"] == 1
    assert second == [{"parsed": 0, "failed": 0, "unsupported": 0}]
    with pg.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM derived_clean_text")).scalar_one() == 1


def test_attachment_links_to_the_fixed_parent_version_of_the_same_run(pg, tmp_path, monkeypatch):
    from clean import derived_text
    from fetch.site_crawler import ATT_SCHEMA, WEB_SCHEMA, attachment_record_id

    page_url = "https://www.nmpa.gov.cn/xxgk/ggtg/2.html"
    att_url = "https://www.nmpa.gov.cn/directory/web/nmpa/images/p.pdf"
    content = b"%PDF-1.4 parent link"
    path = tmp_path / "p.pdf"
    path.write_bytes(content)
    rid = attachment_record_id(page_url, att_url)
    page = json.dumps({"schema_version": WEB_SCHEMA, "role": "web_capture", "site": "nmpa", "url": page_url,
                       "fetched_at": "t", "html_sha256": "0" * 64, "html": "<html></html>",
                       "parsed": {"title": "公告", "publish_date": "2026-11-01", "markdown": "# 公告", "parse_method": "css",
                                  "extra": {}},
                       "attachments": [{"record_id": rid, "url": att_url, "status": "stored"}]}, sort_keys=True)
    att = json.dumps({"schema_version": ATT_SCHEMA, "role": "attachment", "site": "nmpa", "url": att_url,
                      "parent_record_id": page_url, "file_name": "p.pdf", "file_ext": ".pdf",
                      "sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "store_path": str(path),
                      "discovered_from": "detail_page", "status": "stored"}, sort_keys=True)
    request = FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[page_url])
    run, _ = store.create_fetch(request, "link-1")
    store.job_input(run.id, run.attempt)
    done = store.finish_fetch(run.id, run.attempt, {
        "records": [ArchiveRecord(source_record_id=page_url, record_kind="document", raw_content=page),
                    ArchiveRecord(source_record_id=rid, record_kind="document", raw_content=att)],
        "complete": False, "upstream_pagination_exhausted": True, "stop_reason": "upstream_exhausted",
        "requests": 2, "bytes": len(content), "source_total": None, "error": None,
    })
    page_version, att_version = done.version_ids
    from clean import attachment_parse as ap

    monkeypatch.setattr(ap, "_mineru", lambda p: {"success": True, "markdown": "# 正文", "error": "",
                                                  "content_list": [{"type": "text", "text": "正文", "page_idx": 2}]})
    derived_text.process(batch=5, retry_after_seconds=0)
    got = derived_text.for_versions("nmpa", False, [att_version], "current", datetime.now(timezone.utc))
    entry = got["versions"][att_version]
    assert entry["attachment_of"] == [
        {"parent_record_id": page_url, "parent_version_id": page_version, "link_basis": "same_run_parent_version"}
    ]
    assert entry["locator_kind"] == "pdf_page" and entry["segments"][0]["page"] == 3
    assert entry["text"].startswith("〔第3页〕")
    # 按旧库链接补抓（父页面没重读）：父版本未知，如实记录
    legacy, _ = _fetch_attachment(tmp_path, "link-legacy")
    derived_text.process(batch=5, retry_after_seconds=0)
    got = derived_text.for_versions("nmpa", False, [legacy], "current", datetime.now(timezone.utc))
    assert got["versions"][legacy]["attachment_of"][0]["parent_version_id"] is None
    assert got["versions"][legacy]["attachment_of"][0]["link_basis"] == "parent_version_unknown"


# ——GDELT 线索：按观察时间分窗口读，带上各窗口的读取回执——


def test_gdelt_leads_are_listed_by_window_with_the_receipts_of_the_windows(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_FETCH_ENABLED", "true")  # HTTP below is a local test transport.
    import httpx

    from fetch import rate
    from fetch.sources import read_public
    monkeypatch.setattr(rate, "wait_for_url", lambda url, **kw: None)  # the 5 s spacing is not under test

    def response(request):
        # max_records is 2: everything before 00:30 comes back full at every size, the second half does not
        start, end = request.url.params["startdatetime"], request.url.params["enddatetime"]
        return httpx.Response(200, json={"articles": [
            {"url": "https://example.org/" + start + "/" + end + "/" + str(n), "domain": "example.org",
             "title": "SYNTHETIC lead " + start + " " + str(n), "seendate": start[:8] + "T" + start[8:] + "Z"}
            for n in range(2 if start < "20261030003000" else 1)]})
    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(response), **kw))
    request = FetchRequest(source_id="gdelt_doc", query="synthetic", max_records=2,
                           updated_since=date("2026-10-30T00:00:00Z"), updated_until=date("2026-10-30T01:00:00Z"))
    run, _ = store.create_fetch(request, "gdelt-leads-window")
    store.job_input(run.id, run.attempt)
    run = store.finish_fetch(run.id, run.attempt, read_public(request))
    now = datetime.now().astimezone()

    def leads(start, end, **kw):
        return store.list_records("gdelt_doc", False, "operational_replay", now,
                                  window=(date("2026-10-30T" + start + ":00Z"), date("2026-10-30T" + end + ":00Z")), **kw)
    whole = leads("00:00", "01:00")
    assert len(whole.items) == 9  # the nine distinct urls of the five requests
    lead = whole.items[0]
    assert lead.source_url.startswith("https://example.org/") and lead.facts["domain"] == "example.org"
    assert lead.source_updated_at.meaning == "gdelt_first_observed" and lead.title.startswith("SYNTHETIC lead")
    assert [(r["start"][11:16], r["status"], r["possibly_truncated"]) for r in whole.coverage["window_coverage"]] == [
        ("00:00", "split", False), ("00:00", "split", False), ("00:00", "read", True), ("00:15", "read", True),
        ("00:30", "read", False)]
    assert [i["start"][11:16] for i in whole.coverage["truncated_intervals"]] == ["00:00", "00:15"]
    tail = leads("00:30", "01:00")  # the clean half: its leads, and no truncation to report
    assert [i.title for i in tail.items] == ["SYNTHETIC lead 20261030003000 0"]
    assert tail.coverage["truncated_intervals"] == [] and tail.coverage["window_coverage"][-1]["returned"] == 1
    assert leads("02:00", "03:00").items == [] and leads("02:00", "03:00").coverage["window_coverage"] == []
    first = leads("00:00", "01:00", limit=4)
    second = leads("00:00", "01:00", limit=4, cursor=first.next_cursor)
    assert len(second.items) == 4 and not {i.version_id for i in first.items} & {i.version_id for i in second.items}
    unwindowed = store.list_records("gdelt_doc", False, "operational_replay", now)  # no window: all leads, no receipts
    assert len(unwindowed.items) == 9 and "window_coverage" not in unwindowed.coverage
    with pytest.raises(FabricError) as refused:
        store.list_records("gdelt_doc", False, "operational_replay", now, window=(date("2026-10-30T00:00:00Z"), None))
    assert refused.value.code == "invalid_window"
    over_http = _client().get("/api/fabric/records", headers=READ, params={
        "source_id": "gdelt_doc", "is_demo": "false", "mode": "operational_replay", "cutoff": now.isoformat(),
        "window_start": "2026-10-30T00:00:00Z", "window_end": "2026-10-30T00:30:00Z"})
    body = over_http.json()
    assert over_http.status_code == 200 and len(body["items"]) == 8
    assert [i["start"][11:16] for i in body["coverage"]["truncated_intervals"]] == ["00:00", "00:15"]
    assert body["coverage"]["window_coverage"][-1]["possibly_truncated"] is True


def test_gdelt_index_changes_keep_old_original_and_public_material_type(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_FETCH_ENABLED", "true")  # HTTP below is a local test transport.
    import httpx

    from fetch.gdelt_news import article_id
    from fetch.sources import read_public
    title = ["SYNTHETIC first headline"]
    def response(request):
        return httpx.Response(200, json={"articles": [{
            "url": "https://example.org/news?id=1", "title": title[0], "seendate": "20261030T010000Z"}]})
    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(response), **kw))
    request = FetchRequest(source_id="gdelt_doc", query="synthetic", max_records=2)
    old, _ = store.create_fetch(request, "gdelt-integration-a")
    store.job_input(old.id, old.attempt)
    old = store.finish_fetch(old.id, old.attempt, read_public(request))
    title[0] = "SYNTHETIC corrected headline"
    new, _ = store.create_fetch(request, "gdelt-integration-b")
    store.job_input(new.id, new.attempt)
    new = store.finish_fetch(new.id, new.attempt, read_public(request))
    native = article_id({"url": "https://example.org/news?id=1"})
    assert old.version_ids != new.version_ids
    [window] = new.coverage["window_coverage"]  # the receipts are kept in the run's coverage; no window, one receipt
    assert (window["status"], window["returned"], window["possibly_truncated"]) == ("read", 1, False)
    assert "truncated_intervals" not in new.coverage  # an empty list is not recorded
    saved = store.read_version(old.version_ids[0], "gdelt_doc", native, False, "current", datetime.now().astimezone())
    assert saved.clean_text == "SYNTHETIC first headline"
    assert saved.document_evidence["text_level"] == "title"
    assert saved.document_evidence["material_kind"] == "news_report"
    assert saved.availability_proof.kind == "unverified"  # seen date isn't proof of historical availability
