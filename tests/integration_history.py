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


def _graph_sources(monkeypatch):
    values = store.allowed_sources() | {
        "legacy_documents",
        "reviewed_graph",
        "primekg_graph",
    }
    monkeypatch.setenv(
        "DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", json.dumps(sorted(values))
    )


def _synthetic_graph_observation():
    # 明确教学数据，不伪称现役唐已有人工批准关系。
    raw = "药物甲靶向蛋白乙。"
    return {
        "schema_version": "datafabric.legacy.graph.observation.v1",
        "namespace": "fixture.tang",
        "is_demo": True,
        "scope_record_ids": ["demo-doc"],
        "scope_record_count": 1,
        "batch_offset": 0,
        "selected_document_uuids": ["demo-doc"],
        "next_document_offset": None,
        "selection_truncated": False,
        "documents": [
            {
                "uuid": "demo-doc",
                "source_site": "external_archive",
                "title": "明确合成的审核原件",
                "raw_content": raw,
            }
        ],
        "entities": [
            {
                "id": 1,
                "source_doc_uuid": "demo-doc",
                "entity_type": "Drug",
                "entity_name": "药物甲",
                "dict_id": 10,
                "source_text": "药物甲",
                "review_status": "approved",
            },
            {
                "id": 2,
                "source_doc_uuid": "demo-doc",
                "entity_type": "Target",
                "entity_name": "蛋白乙",
                "dict_id": 20,
                "source_text": "蛋白乙",
                "review_status": "approved",
            },
        ],
        "relations": [
            {
                "id": 3,
                "source_doc_uuid": "demo-doc",
                "from_entity_id": 1,
                "to_entity_id": 2,
                "relation_type": "TARGETS",
                "relation_props": {},
                "source_text": raw,
                "review_status": "approved",
            }
        ],
        "verified_dict_ids": ["10", "20"],
        "merge_logs": [],
    }


def _published_graph(pg, monkeypatch, key="graph-fixture"):
    from flows.graph_publish import publish_legacy

    _graph_sources(monkeypatch)
    observation = _synthetic_graph_observation()
    run = publish_legacy(observation, key)
    with pg.begin() as conn:
        payload = conn.execute(
            text("SELECT payload FROM history_versions WHERE version_id=:id"),
            {"id": run.version_ids[0]},
        ).scalar_one()
        now = store._now(conn)
    return observation, run, payload, now


def _graph_archive(payload, **extra):
    return ArchiveImport(
        source_id="reviewed_graph",
        is_demo=True,
        scope_description="explicit synthetic graph",
        declared_count=1,
        declared_complete=False,
        records=[
            ArchiveRecord(
                source_record_id=payload["source_record_id"],
                record_kind="graph",
                raw_content=payload["raw_content"],
            )
        ],
        **extra,
    )


def test_graph_roundtrip_unicode_quote_and_idempotency(pg, monkeypatch):
    from biz_rules.reviewed_graph import GraphBundle
    from flows.graph_publish import publish_legacy

    observation, run, payload, now = _published_graph(pg, monkeypatch)
    graph = GraphBundle.model_validate_json(payload["raw_content"])
    assert graph.edges[0].binding_status == "bound"
    assert graph.edges[0].evidence[0].quote == "药物甲靶向蛋白乙。"
    assert graph.nodes[0].identity_status == "confirmed"
    assert publish_legacy(observation, "graph-fixture").version_ids == run.version_ids
    result = store.read_graph_neighborhood(
        run.version_ids[0],
        "reviewed_graph",
        payload["source_record_id"],
        True,
        "current",
        now,
        seed_ids=["fixture.tang.entity_dict|10"],
        direction="out",
    )
    assert len(result.edges) == 1 and result.edges[0].target.id == "20"
    assert result.graph_ref.version_id == run.version_ids[0]
    assert result.coverage.scope == "selected_published_subgraph"
    resolved = store.read_graph_resolution(
        run.version_ids[0],
        "reviewed_graph",
        payload["source_record_id"],
        True,
        "current",
        now,
        name="药物甲",
    )
    assert resolved.status == "resolved" and resolved.candidates[0].id == "10"


def test_graph_quote_tamper_is_rejected_and_previous_versions_remain(pg, monkeypatch):
    from biz_rules.reviewed_graph import GraphBundle

    _, _, payload, now = _published_graph(pg, monkeypatch)
    graph = GraphBundle.model_validate_json(payload["raw_content"])
    graph.edges[0].evidence[0].quote = "伪造证据"
    invalid = {**payload, "raw_content": graph.model_dump_json()}
    with pytest.raises(Exception) as error:
        store.import_archive(_graph_archive(invalid), "bad-quote")
    assert error.value.code == "graph_quote_mismatch"
    unchanged = store.read_version(
        payload["version_id"],
        "reviewed_graph",
        payload["source_record_id"],
        True,
        "current",
        now,
    )
    assert unchanged.raw_sha256 == payload["raw_sha256"]
    with pg.begin() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM history_versions WHERE record_kind='graph'")
            ).scalar_one()
            == 1
        )


def test_graph_dependencies_revoke_normal_version_raw_and_neighborhood(pg, monkeypatch):
    _, _, payload, now = _published_graph(pg, monkeypatch)
    with pg.begin() as conn:
        source = conn.execute(
            text("SELECT catalog FROM history_sources WHERE source_id='reviewed_graph'")
        ).scalar_one()
        assert set(source["dependency_source_ids"]) == {
            "legacy_documents",
            "external_archive",
        }
        conn.execute(
            text(
                "UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"
            )
        )
    available = {row["source_id"] for row in store.list_sources()}
    assert "reviewed_graph" not in available and "legacy_documents" not in available
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    params = {
        "source_id": "reviewed_graph",
        "source_record_id": payload["source_record_id"],
        "is_demo": "true",
        "mode": "current",
        "cutoff": now.isoformat(),
    }
    headers = {"Authorization": "Bearer test-reader-only"}
    for path in (
        "/api/history/versions/" + payload["version_id"],
        "/api/history/versions/" + payload["version_id"] + "/raw",
        "/api/history/graph/" + payload["version_id"] + "/neighborhood",
        "/api/history/graph/" + payload["version_id"] + "/resolve",
    ):
        response = client.get(
            path,
            params={
                **params,
                "seed_ids": "fixture.tang.entity_dict|10",
                "name": "药物甲",
            },
            headers=headers,
        )
        assert response.status_code == 403 and "药物甲" not in response.text


def test_current_graph_cannot_gain_old_archive_proof_or_future_merge(pg, monkeypatch):
    from datetime import timedelta

    from biz_rules.reviewed_graph import GraphBundle, GraphRedirect

    _, _, payload, now = _published_graph(pg, monkeypatch)
    request = _graph_archive(payload)
    from biz_rules.history_models import ArchiveProofRequest, TimeValue

    request.proof = ArchiveProofRequest(
        available_at=TimeValue(value="2016-01-01", precision="day"),
        uri="fixture://graph-history",
        sha256=archive_manifest_hash(request.records),
        reviewed=True,
        review_note="synthetic unsupported graph proof",
    )
    with pytest.raises(Exception) as error:
        store.import_archive(request, "bad-old-graph-proof")
    assert error.value.code == "graph_history_unsupported"
    graph = GraphBundle.model_validate_json(payload["raw_content"])
    graph.redirects = [
        GraphRedirect(
            source={"namespace": "fixture.tang.entity_dict", "id": "9"},
            target={"namespace": "fixture.tang.entity_dict", "id": "10"},
            merge_log_id="fixture",
            merged_at=now + timedelta(days=1),
        )
    ]
    # 外部模型重新校验，避免绕开入口验证。
    graph = GraphBundle.model_validate(graph.model_dump())
    changed = {**payload, "raw_content": graph.model_dump_json()}
    with pytest.raises(Exception) as error:
        store.import_archive(_graph_archive(changed), "bad-future-merge")
    assert error.value.code == "future_graph_merge"


def test_graph_reads_reject_a_bound_document_not_visible_at_same_cutoff(
    pg, monkeypatch
):
    from datetime import timedelta

    from biz_rules.history_models import canonical, sha256

    _, _, payload, now = _published_graph(pg, monkeypatch)
    old_time = now - timedelta(days=2)
    # 模拟已有旧版错误包的日期：只在临时schema插入，验证读取边界不会沿用包资格带入后见原件。
    corrupted = {
        **payload,
        "version_id": sha256(payload["version_id"] + "explicit-test-old-date"),
        "first_seen_at": old_time.isoformat(),
        "received_at": old_time.isoformat(),
    }
    with pg.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO history_versions(version_id,source_id,is_demo,source_record_id,record_kind,first_seen_at,received_at,proof_available_at,published_seq,payload)
            VALUES(:id,'reviewed_graph',true,:native,'graph',:first,:first,NULL,999,CAST(:payload AS jsonb))
        """),
            {
                "id": corrupted["version_id"],
                "native": corrupted["source_record_id"],
                "first": old_time,
                "payload": canonical(corrupted),
            },
        )
    with pytest.raises(Exception) as error:
        store.read_version(
            corrupted["version_id"],
            "reviewed_graph",
            corrupted["source_record_id"],
            True,
            "operational_replay",
            old_time + timedelta(hours=1),
        )
    assert error.value.code == "graph_evidence_not_visible"


def test_primekg_only_replays_background_after_local_first_seen(pg, monkeypatch):
    from flows.graph_publish import publish_primekg

    _graph_sources(monkeypatch)
    capture = {
        "is_demo": False,
        "source": "primekg_current_projection",
        "observed_at": "2026-10-18T00:00:00Z",
        "seed": {"node_source": "NCBI", "primekg_id": "1956"},
        "has_more": True,
        "rows": [
            {
                "from_node": {
                    "name": "EGFR",
                    "source": "NCBI",
                    "id": "1956",
                    "type": "gene/protein",
                },
                "to_node": {
                    "name": "synthetic disease",
                    "source": "MONDO",
                    "id": "1",
                    "type": "disease",
                },
                "relation_type": "disease_protein",
                "properties": {"display_relation": "associated with"},
            }
        ],
    }
    run = publish_primekg(capture, "synthetic-primekg-shape")
    with pg.begin() as conn:
        row = conn.execute(
            text("SELECT payload FROM history_versions WHERE version_id=:id"),
            {"id": run.version_ids[0]},
        ).scalar_one()
        now = store._now(conn)
    result = store.read_graph_resolution(
        run.version_ids[0],
        "primekg_graph",
        row["source_record_id"],
        False,
        "current",
        now,
        external_namespace="NCBI",
        external_id="1956",
    )
    assert (
        result.status == "resolved"
        and result.candidates[0].identity_status == "background"
    )
    replayed = store.read_version(
        run.version_ids[0],
        "primekg_graph",
        row["source_record_id"],
        False,
        "operational_replay",
        now,
    )
    assert (
        replayed.version_id == run.version_ids[0]
        and replayed.facts["graph_kind"] == "background"
    )
    with pytest.raises(Exception) as error:
        store.read_version(
            run.version_ids[0],
            "primekg_graph",
            row["source_record_id"],
            False,
            "operational_replay",
            date("2018-01-01T00:00:00Z"),
        )
    assert error.value.code == "version_not_found"
    with pytest.raises(Exception) as error:
        store.read_version(
            run.version_ids[0],
            "primekg_graph",
            row["source_record_id"],
            False,
            "public_history_reconstruction",
            now,
        )
    assert error.value.code == "graph_history_unsupported"


def test_legacy_document_cannot_smuggle_an_unlicensed_original_source(pg, monkeypatch):
    from flows.graph_publish import publish_legacy

    _graph_sources(monkeypatch)
    observation = _synthetic_graph_observation()
    observation["documents"][0]["source_site"] = "not-in-catalog"
    with pytest.raises(Exception) as error:
        publish_legacy(observation, "bad-original-source")
    assert error.value.code in {"source_not_allowed", "source_not_available"}
    with pg.begin() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one()
            == 0
        )


def test_graph_manifest_cannot_bind_demo_document_into_real_package(pg, monkeypatch):
    _, _, payload, _ = _published_graph(pg, monkeypatch)
    request = _graph_archive(payload)
    request.is_demo = False
    with pytest.raises(Exception) as error:
        store.import_archive(request, "demo-to-real-graph")
    assert error.value.code == "graph_demo_mismatch"


def test_changes_head_sidecar_uses_source_time_and_frozen_published_boundary(pg):
    newer = store.import_archive(
        archive([raw_record("late-adoption", "PHASE2")], "2018-06-01"), "sidecar-newer"
    )
    store.import_archive(
        archive([raw_record("late-adoption", "PHASE1")], "2016-06-01"), "sidecar-older"
    )
    first = store.list_changes("external_archive", True, limit=1)
    assert first["current_version_ids"]["late-adoption"] == newer.version_ids[0]
    assert first["next_cursor"] and first["max_seq"] == 2
    latest = store.import_archive(
        archive([raw_record("late-adoption", "PHASE3")], "2020-06-01"),
        "sidecar-after-page",
    )
    next_page = store.list_changes(
        "external_archive", True, limit=1, cursor=first["next_cursor"]
    )
    assert next_page["max_seq"] == 2
    assert next_page["current_version_ids"]["late-adoption"] == newer.version_ids[0]
    assert latest.version_ids[0] != newer.version_ids[0]


def _fetch_europe_pmc_revision(abstract, key):
    """固定合法来源输入，只替代网络请求；使用真实抓取任务和事务发布入口。"""
    raw = json.dumps({"source": "MED", "id": "123", "title": "Example",
                      "abstractText": abstract}, sort_keys=True)
    request = FetchRequest(source_id="europe_pmc", query="synthetic return regression")
    run, fresh = store.create_fetch(request, key)
    if not fresh:
        return run
    store.job_input(run.id, run.attempt)
    return store.finish_fetch(run.id, run.attempt, {
        "records": [ArchiveRecord(source_record_id="MED:123", record_kind="document",
                                  raw_content=raw)],
        "complete": False, "upstream_pagination_exhausted": True,
        "stop_reason": "end", "requests": 1, "bytes": len(raw.encode()),
        "source_total": 1, "error": None,
    })


def test_public_fetch_restored_content_is_a_new_adoption_not_a_duplicate(pg):
    runs, sequences = [], []
    for index, abstract in enumerate(["A", "B", "A", "A"]):
        runs.append(_fetch_europe_pmc_revision(abstract, f"return-{index}"))
        with pg.begin() as conn:
            sequences.append(conn.execute(text(
                "SELECT published_seq FROM history_sources WHERE source_id='europe_pmc'"
            )).scalar_one())
        if index == 1:
            frozen = store.list_changes("europe_pmc", False, limit=1)
    cutoff = datetime.now().astimezone()
    current = store.list_records("europe_pmc", False, "current", cutoff)
    changes = store.list_changes("europe_pmc", False, after=2)
    print(json.dumps({
        "sequence": sequences, "run_states": [run.state for run in runs],
        "new_version_counts": [run.coverage["new_version_count"] for run in runs],
        "current_abstract": current.items[0].facts["abstract"],
        "changes_after_2": [item["published_seq"] for item in changes["items"]],
    }, ensure_ascii=False))

    assert sequences == [1, 2, 3, 3]
    assert all(run.state == "completed" for run in runs)
    assert [run.coverage["new_version_count"] for run in runs] == [1, 1, 1, 0]
    assert runs[2].version_ids != runs[0].version_ids
    assert runs[3].version_ids == runs[2].version_ids
    assert current.items[0].facts["abstract"] == "A"
    assert current.items[0].version_id == runs[2].version_ids[0]
    assert [item["published_seq"] for item in changes["items"]] == [3]
    assert changes["current_version_ids"]["MED:123"] == runs[2].version_ids[0]
    assert changes["next_after"] == 3
    continued = store.list_changes("europe_pmc", False, limit=1, cursor=frozen["next_cursor"])
    assert continued["max_seq"] == 2
    assert continued["items"][0]["version_id"] == runs[1].version_ids[0]
    assert continued["current_version_ids"]["MED:123"] == runs[1].version_ids[0]

    # 旧版本内容/编号/首次看到时间不可被恢复操作改写。
    fixed = [store.read_version(run.version_ids[0], "europe_pmc", "MED:123",
                                False, "current", cutoff) for run in runs[:3]]
    assert [record.facts["abstract"] for record in fixed] == ["A", "B", "A"]
    assert fixed[0].raw_sha256 == fixed[2].raw_sha256
    assert all(record.source_updated_at is None and record.source_published_at is None
               for record in fixed)
    assert fixed[0].first_seen_at < fixed[1].first_seen_at < fixed[2].first_seen_at
    past = store.list_records("europe_pmc", False, "operational_replay",
                              runs[1].completed_at)
    assert past.items[0].version_id == runs[1].version_ids[0]
    assert store.list_records(
        "europe_pmc", False, "public_history_reconstruction", cutoff
    ).items == []  # 本次观察不自动变成历史可见证明。
    retry = _fetch_europe_pmc_revision("A", "return-2")
    assert retry.id == runs[2].id and retry.version_ids == runs[2].version_ids
    assert store.list_changes("europe_pmc", False, after=3)["items"] == []


def test_concurrent_public_restorations_publish_once_and_old_run_retry_is_read_only(pg):
    first = _fetch_europe_pmc_revision("A", "concurrent-first")
    same = _fetch_europe_pmc_revision("A", "concurrent-same")
    assert same.version_ids == first.version_ids
    assert same.coverage["new_version_count"] == 0
    changed = _fetch_europe_pmc_revision("B", "concurrent-changed")
    repeated = _fetch_europe_pmc_revision("A", "concurrent-first")
    assert repeated.id == first.id
    before = store.list_records("europe_pmc", False, "current", datetime.now().astimezone())
    assert before.items[0].version_id == changed.version_ids[0]
    assert store.list_changes("europe_pmc", False)["max_seq"] == 2

    with ThreadPoolExecutor(max_workers=2) as pool:
        restored = list(pool.map(
            lambda index: _fetch_europe_pmc_revision("A", f"concurrent-restore-{index}"),
            [1, 2],
        ))
    assert restored[0].version_ids == restored[1].version_ids
    assert restored[0].version_ids != first.version_ids
    assert sorted(run.coverage["new_version_count"] for run in restored) == [0, 1]
    changes = store.list_changes("europe_pmc", False)
    assert changes["max_seq"] == 3 and len(changes["items"]) == 3
    assert changes["current_version_ids"]["MED:123"] == restored[0].version_ids[0]
    assert [item["facts"]["abstract"] for item in changes["items"]] == ["A", "B", "A"]


def _fetch_trial(holder, key, status="RECRUITING"):
    """A ClinicalTrials study as the API returns it, stamped with the API's data date (versionHolder)."""
    raw = json.dumps({
        "protocolSection": {
            "identificationModule": {"nctId": "NCT00000123", "briefTitle": "Example trial"},
            "statusModule": {"overallStatus": status},
        },
        "derivedSection": {"miscInfoModule": {"versionHolder": holder}},
    }, sort_keys=True)
    request = FetchRequest(source_id="clinicaltrials", query="synthetic data date regression")
    run, fresh = store.create_fetch(request, key)
    if not fresh:
        return run
    store.job_input(run.id, run.attempt)
    return store.finish_fetch(run.id, run.attempt, {
        "records": [ArchiveRecord(source_record_id="NCT00000123", record_kind="trial", raw_content=raw)],
        "complete": False, "upstream_pagination_exhausted": True,
        "stop_reason": "end", "requests": 1, "bytes": len(raw.encode()),
        "source_total": 1, "error": None,
    })


def _trial_versions(pg):
    with pg.begin() as conn:
        return conn.execute(text(
            "SELECT version_id, payload->>'raw_content', payload->>'raw_sha256', first_seen_at, "
            "payload->'facts'->>'status' FROM history_versions "
            "WHERE source_id='clinicaltrials' AND source_record_id='NCT00000123' ORDER BY published_seq"
        )).all()


def test_a_new_clinicaltrials_data_date_alone_is_not_a_new_version(pg):
    first = _fetch_trial("2026-11-02", "holder-1")
    before = _trial_versions(pg)
    for day, key in (("2026-11-03", "holder-2"), ("2026-11-04", "holder-3")):
        run = _fetch_trial(day, key)
        assert run.version_ids == first.version_ids
    after = _trial_versions(pg)
    assert len(after) == 1 and after == before  # raw, raw_sha256 and first_seen stay as first seen
    assert '"2026-11-02"' in after[0][1]


def test_a_real_change_or_a_return_is_still_a_new_version(pg):
    a = _fetch_trial("2026-11-02", "real-1")
    b = _fetch_trial("2026-11-03", "real-2", status="COMPLETED")
    back = _fetch_trial("2026-11-04", "real-3")  # A again, on a later data date
    again = _fetch_trial("2026-11-05", "real-4")
    versions = _trial_versions(pg)
    assert [v[4] for v in versions] == ["RECRUITING", "COMPLETED", "RECRUITING"]
    assert len({a.version_ids[0], b.version_ids[0], back.version_ids[0]}) == 3  # A->B->A adopts anew
    assert again.version_ids == back.version_ids  # and then the data date alone changes nothing


def test_an_unknown_data_date_shape_is_compared_as_it_is(pg):
    _fetch_trial("2026-11-02", "shape-1")
    _fetch_trial({"date": "2026-11-03"}, "shape-2")
    assert len(_trial_versions(pg)) == 2
