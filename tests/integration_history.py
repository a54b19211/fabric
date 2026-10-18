"""真实PostgreSQL隔离schema；不使用SQLite，不碰现役表或parent E2E记录。"""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from api.routers.history import router
from biz_rules.history_models import (
    ArchiveImport,
    ArchiveRecord,
    FetchRequest,
    archive_manifest_hash,
)
from db import history as store
from migration import history as migration


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def raw_record(native, phase="PHASE1", **extra):
    data = {
        "record_kind": "trial",
        "title": native,
        "trial_id": native,
        "status": "RECRUITING",
        "phase": [phase],
        **extra,
    }
    return ArchiveRecord(
        source_record_id=native,
        record_kind=data["record_kind"],
        raw_content=json.dumps(data, ensure_ascii=False),
    )


def archive(
    records, available="2016-06-01", *, reviewed=True, complete=True, scope_ids=()
):
    proof = {
        "available_at": {"value": available, "precision": "day"},
        "uri": "fixture://explicit-demo-archive",
        "sha256": archive_manifest_hash(records),
        "reviewed": reviewed,
        "review_note": "Explicit synthetic test archive and dates",
    }
    return ArchiveImport(
        source_id="external_archive",
        is_demo=True,
        scope_description="explicit synthetic cohort",
        records=records,
        declared_count=len(records),
        declared_complete=complete,
        proof=proof,
        scope_record_ids=list(scope_ids),
    )


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL URL required")
    parsed = make_url(url)
    assert (
        parsed.host == "127.0.0.1"
        and parsed.port == 18436
        and parsed.database == "fabric_history"
    )
    engine = create_engine(url)
    schema = "test_history_" + uuid.uuid4().hex
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
    monkeypatch.setenv(
        "DATAFABRIC_HISTORY_SOURCE_ALLOWLIST",
        '["external_archive","clinicaltrials","pubmed","fda_crl","europe_pmc"]',
    )
    monkeypatch.setenv("DATAFABRIC_HISTORY_READ_TOKEN", "test-reader-only")
    monkeypatch.setenv("DATAFABRIC_HISTORY_ADMIN_TOKEN", "test-admin-only")
    monkeypatch.setattr(store, "history_engine", lambda: pg_engine)
    monkeypatch.setattr(migration, "history_engine", lambda: pg_engine)
    migration.migrate_history()
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE history_versions,history_runs"))
        conn.execute(
            text("UPDATE history_sources SET published_seq=0,read_enabled=true")
        )
    return pg_engine


def test_immutable_versions_idempotency_and_no_receipt_backdating(pg):
    request = archive([raw_record("T1")])
    first = store.import_archive(request, "archive-first")
    repeated = store.import_archive(request, "archive-first")
    other_key = store.import_archive(request, "archive-other-key")
    assert repeated.id == first.id
    assert other_key.version_ids == first.version_ids
    old = store.read_version(
        first.version_ids[0],
        "external_archive",
        "T1",
        True,
        "public_history_reconstruction",
        date("2016-12-31T23:59:59Z"),
    )
    assert old.first_seen_at.year >= 2026
    with pg.begin() as conn:
        assert (
            conn.execute(
                text("UPDATE history_versions SET source_record_id='CORRUPTED'")
            ).rowcount
            == 0
        )
        assert conn.execute(text("DELETE FROM history_versions")).rowcount == 0
        assert (
            conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one()
            == 1
        )
    with pytest.raises(Exception, match="不同采用请求"):
        store.import_archive(archive([raw_record("T2")]), "archive-first")
    with pytest.raises(Exception, match="不可见"):
        store.read_version(
            old.version_id,
            "external_archive",
            "T1",
            True,
            "operational_replay",
            date("2016-12-31T23:59:59Z"),
        )


def test_eligible_before_latest_and_late_old_archive_does_not_replace_new(pg):
    later = store.import_archive(
        archive([raw_record("T1", "PHASE2")], "2018-06-01"), "later-first"
    )
    earlier = store.import_archive(
        archive([raw_record("T1", "PHASE1")], "2016-06-01"), "earlier-backfill"
    )
    old = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2017-01-01T00:00:00Z"),
    )
    new = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2019-01-01T00:00:00Z"),
    )
    assert old.items[0].version_id == earlier.version_ids[0]
    assert new.items[0].version_id == later.version_ids[0]
    assert not old.coverage.complete  # 未声明manifest不冒充全量


def test_frozen_pagination_and_current_permission_rechecked(pg, monkeypatch):
    initial = store.import_archive(
        archive([raw_record("A"), raw_record("B"), raw_record("C")]), "initial"
    )
    first = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2020-01-01T00:00:00Z"),
        run_id=initial.id,
        limit=1,
    )
    store.import_archive(archive([raw_record("B", "PHASE2")], "2018-01-01"), "B-update")
    second = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2020-01-01T00:00:00Z"),
        run_id=initial.id,
        limit=1,
        cursor=first.next_cursor,
    )
    assert second.items[0].facts["phase"] == ["PHASE1"]
    with pytest.raises(Exception, match="游标不匹配"):
        store.list_records(
            "external_archive",
            True,
            "public_history_reconstruction",
            date("2019-01-01T00:00:00Z"),
            run_id=initial.id,
            limit=1,
            cursor=first.next_cursor,
        )
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", "[]")
    with pytest.raises(Exception, match="许可"):
        store.read_run(initial.id)
    with pytest.raises(Exception, match="许可"):
        store.read_version(
            first.items[0].version_id,
            "external_archive",
            "A",
            True,
            "public_history_reconstruction",
            date("2020-01-01T00:00:00Z"),
        )


def test_manifest_scope_and_proof_cutoff_cannot_fake_completeness(pg):
    store.import_archive(archive([raw_record("A")]), "A-early")
    manifest = store.import_archive(
        archive([raw_record("B")], "2018-01-01", scope_ids=["A", "B"]), "scope-full"
    )
    response = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2017-01-01T00:00:00Z"),
        run_id=manifest.id,
    )
    assert [item.source_record_id for item in response.items] == ["A"]
    assert response.coverage.not_visible_in_scope == 1
    assert not response.coverage.complete
    full = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2019-01-01T00:00:00Z"),
        run_id=manifest.id,
    )
    assert full.coverage.complete
    selected = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2019-01-01T00:00:00Z"),
        run_id=manifest.id,
        source_record_ids=["A", "OUTSIDE"],
    )
    assert len(selected.items) == 1 and not selected.coverage.complete


def test_unverified_and_future_outcome_or_mapping_do_not_gain_history_rights(pg):
    run = store.import_archive(archive([raw_record("U")], reviewed=False), "unverified")
    assert not store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2017-01-01T00:00:00Z"),
        run_id=run.id,
    ).items
    event = raw_record(
        "O1",
        record_kind="outcome",
        event_type="trial_stoppage",
        unit_kind="trial",
        unit_id="T1",
        event_observed=True,
        event_at="2020-01-01",
        public_available_at="2021-01-01",
    )
    with pytest.raises(Exception, match="晚于归档"):
        store.import_archive(archive([event], "2016-01-01"), "future-outcome")
    program = raw_record(
        "P1",
        record_kind="program",
        program_id="P1",
        lineage_id="L1",
        indication="disease",
        jurisdiction="US",
        mapping_available_at="2019-01-01",
    )
    request = archive([program], "2016-01-01")
    request.verified_mapping_ids = ["P1"]
    request.mapping_review_basis = "explicit synthetic mapping"
    with pytest.raises(Exception, match="晚于归档"):
        store.import_archive(request, "future-mapping")


def test_query_literal_and_latest_not_old_matching_version(pg):
    store.import_archive(
        archive([raw_record("T1", summary="old matchedword")], "2016-01-01"), "old-word"
    )
    store.import_archive(
        archive([raw_record("T1", summary="new content")], "2018-01-01"), "new-word"
    )
    latest = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2019-01-01T00:00:00Z"),
        terms=["matchedword"],
    )
    assert latest.items == []
    assert (
        store.list_records(
            "external_archive",
            True,
            "public_history_reconstruction",
            date("2019-01-01T00:00:00Z"),
            terms=["%_"],
        ).items
        == []
    )


def test_changes_freeze_commit_boundary_and_concurrent_publish(pg):
    def submit(native):
        return store.import_archive(
            archive([raw_record(native)]), "concurrent-" + native
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(submit, ["A", "B"]))
    changes = store.list_changes("external_archive", True, limit=1)
    store.import_archive(archive([raw_record("C")]), "after-first-page")
    next_page = store.list_changes(
        "external_archive", True, limit=1, cursor=changes["next_cursor"]
    )
    assert next_page["max_seq"] == changes["max_seq"] == 2
    assert next_page["next_cursor"] is None
    assert next_page["next_after"] == 2
    following = store.list_changes("external_archive", True, after=2)
    assert [item["source_record_id"] for item in following["items"]] == ["C"]


def test_fetch_attempt_cas_prevents_stale_publish(pg):
    run, _fresh = store.create_fetch(
        FetchRequest(source_id="europe_pmc", query="obesity"), "fetch-cas"
    )
    store.job_input(run.id, 1)
    retried = store.retry_fetch(run.id, 1, job_inactive=True)
    batch = {
        "records": [],
        "complete": False,
        "upstream_pagination_exhausted": True,
        "stop_reason": "upstream_no_match",
        "requests": 1,
        "bytes": 10,
        "source_total": 0,
        "error": None,
    }
    with pytest.raises(Exception, match="旧任务"):
        store.finish_fetch(run.id, 1, batch)
    store.job_input(run.id, 2)
    finished = store.finish_fetch(run.id, 2, batch)
    assert finished.state == "completed" and finished.attempt == retried.attempt == 2


def test_http_auth_real_demo_receipt_and_raw_permissions(pg, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/history/sources").status_code == 401
    read = {"Authorization": "Bearer test-reader-only"}
    admin = {
        "Authorization": "Bearer test-admin-only",
        "Idempotency-Key": "http-archive",
    }
    body = archive([raw_record("A")]).model_dump(mode="json")
    assert (
        client.post("/api/history/archives", json=body, headers=read).status_code == 403
    )
    bad = {**body, "first_seen_at": "2016-01-01T00:00:00Z"}
    assert (
        client.post("/api/history/archives", json=bad, headers=admin).status_code == 422
    )
    result = client.post("/api/history/archives", json=body, headers=admin)
    assert result.status_code == 200, result.text
    run = result.json()
    params = {
        "source_id": "external_archive",
        "source_record_id": "A",
        "is_demo": "true",
        "mode": "public_history_reconstruction",
        "cutoff": "2017-01-01T00:00:00Z",
    }
    uri = "/api/history/versions/" + run["version_ids"][0]
    record = client.get(uri, params=params, headers=read)
    assert record.status_code == 200
    raw = client.get(uri + "/raw", params=params, headers=read)
    assert raw.text == body["records"][0]["raw_content"]
    assert raw.headers["x-content-sha256"] == record.json()["raw_sha256"]
    assert (
        client.get(uri, params={**params, "is_demo": "false"}, headers=read).status_code
        == 404
    )
    assert (
        client.get(
            uri, params={**params, "cutoff": "2035-01-01T00:00:00Z"}, headers=read
        ).status_code
        == 422
    )
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", "[]")
    assert client.get(uri + "/raw", params=params, headers=read).status_code == 403


def test_same_history_time_conflict_is_rejected_or_source_revision_orders_it(pg):
    first = store.import_archive(
        archive([raw_record("T1", "PHASE1")]), "ambiguous-first"
    )
    with pytest.raises(Exception, match="无法确定修订先后"):
        store.import_archive(archive([raw_record("T1", "PHASE2")]), "ambiguous-second")
    with pg.begin() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one()
            == 1
        )
        assert conn.execute(text("SELECT count(*) FROM history_runs")).scalar_one() == 1
    same = store.import_archive(archive([raw_record("T1", "PHASE1")]), "same-original")
    assert same.version_ids == first.version_ids

    newer = store.import_archive(
        archive([raw_record("T2", "PHASE2", source_updated_at="2015-06-01")]),
        "newer-source-revision",
    )
    older = store.import_archive(
        archive([raw_record("T2", "PHASE1", source_updated_at="2015-01-01")]),
        "older-late-backfill",
    )
    selected = store.list_records(
        "external_archive",
        True,
        "public_history_reconstruction",
        date("2017-01-01T00:00:00Z"),
        source_record_ids=["T2"],
    )
    assert selected.items[0].version_id == newer.version_ids[0]
    preserved = store.read_version(
        older.version_ids[0],
        "external_archive",
        "T2",
        True,
        "public_history_reconstruction",
        date("2017-01-01T00:00:00Z"),
    )
    assert preserved.facts["phase"] == ["PHASE1"]


def test_overlapping_coarse_source_revision_times_cannot_invent_order(pg):
    store.import_archive(
        archive([raw_record("T1", "PHASE2", source_updated_at="2015-06-01")]),
        "day-revision",
    )
    with pytest.raises(Exception, match="无法确定修订先后"):
        store.import_archive(
            archive([raw_record("T1", "PHASE1", source_updated_at="2015")]),
            "overlapping-year",
        )
    with pg.begin() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one()
            == 1
        )


def test_overlapping_archive_proof_precision_is_not_a_revision_order(pg):
    request = archive([raw_record("T1", "PHASE1")])
    request.proof.available_at.value = "2015"
    request.proof.available_at.precision = "year"
    store.import_archive(request, "coarse-year-proof")
    with pytest.raises(Exception, match="时间精度重叠"):
        store.import_archive(
            archive([raw_record("T1", "PHASE2")], "2015-06-01"), "overlapping-day-proof"
        )
    with pg.begin() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one()
            == 1
        )
