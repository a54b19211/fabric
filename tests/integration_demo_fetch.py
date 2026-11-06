"""Real PostgreSQL demo fetch checks; opt in with this run's creation receipt.

RQ/HTTP process integration is separately recorded by the owning test runner.
No public source or model is contacted here, and only a new test schema is changed.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from biz_rules.history_models import ArchiveRecord, FetchRequest, HistoryError
from db import history as store
from flows import history_fetch as flow
from migration import history as migration


def original(native="SYNTHETIC-DEMO-1", summary="SYNTHETIC original report"):
    return ArchiveRecord(source_record_id=native, record_kind="document", raw_content=json.dumps({
        "record_kind": "document", "title": native, "summary": summary,
        "source_published_at": "2020-01-01", "material_type": "social_media_post",
        "source_reference": "fixture://r5-isolated-test/" + native,
    }))


@pytest.fixture(scope="module")
def isolated_pg():
    receipt_path = os.environ.get("DATAFABRIC_DEMO_TEST_RESOURCE_RECEIPT")
    database_url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    raw_config = os.environ.get("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG")
    if not receipt_path or not database_url or not raw_config:
        pytest.skip("Explicit demo resource creation receipt and isolated configuration required")
    receipt = json.loads(Path(receipt_path).read_text())
    config = json.loads(raw_config)
    parsed = make_url(database_url)
    assert receipt["created_by_this_run"] is True and receipt["existing_resources_used"] is False
    assert receipt["purpose"] == "isolated Fabric demo fetch contract regression; no real model/public crawl"
    assert receipt["run_id"] == config["instance_id"]
    resources = {item["role"]: item for item in receipt["resources"]}
    assert resources["postgres"]["host"] == resources["redis"]["host"] == "127.0.0.1"
    assert (parsed.host, parsed.port, parsed.database) == (
        resources["postgres"]["host"], resources["postgres"]["port"], receipt["database_name"])
    assert (config["redis"]["host"], config["redis"]["port"]) == (
        resources["redis"]["host"], resources["redis"]["port"])
    flow.require_demo_fetch(SimpleNamespace(is_demo=True, demo_instance_id=config["instance_id"]))
    owner = create_engine(database_url)
    schema = "r5_demo_fetch_" + uuid.uuid4().hex
    with owner.begin() as conn:
        conn.execute(text("CREATE SCHEMA " + schema))
    scoped = create_engine(database_url, connect_args={"options": "-csearch_path=" + schema})
    yield scoped
    scoped.dispose()
    with owner.begin() as conn:
        conn.execute(text("DROP SCHEMA " + schema + " CASCADE"))
    owner.dispose()


@pytest.fixture
def demo(isolated_pg, monkeypatch, tmp_path):
    config = json.loads(os.environ["DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG"])
    path = tmp_path / "current-visible-synthetic-input.json"
    config["input_file"] = str(path)
    payload = {"schema_version": "datafabric.demo_fetch.v1", "instance_id": config["instance_id"],
               "sources": {"external_archive": [original().model_dump()]}}
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG", json.dumps(config))
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["external_archive"]')
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "false")
    monkeypatch.setenv("DATAFABRIC_HISTORY_READ_TOKEN", "r5-test-read-only")
    monkeypatch.setattr(store, "history_engine", lambda: isolated_pg)
    monkeypatch.setattr(migration, "history_engine", lambda: isolated_pg)
    forbidden = Mock(side_effect=AssertionError("Demo cannot contact public sources"))
    monkeypatch.setattr(flow, "read_public", forbidden)
    migration.migrate_history()
    with isolated_pg.begin() as conn:
        conn.execute(text("TRUNCATE history_versions,history_runs CASCADE"))
        conn.execute(text("UPDATE history_sources SET published_seq=0,read_enabled=true"))
    request = FetchRequest(source_id="external_archive", query="*", is_demo=True,
                           demo_instance_id=config["instance_id"])
    return SimpleNamespace(engine=isolated_pg, request=request, path=path,
                           payload=payload, forbidden=forbidden)


def perform(demo, key):
    run, fresh = store.create_fetch(demo.request, key)
    assert fresh
    flow.history_fetch_job(run.id, run.attempt, demo_instance_id=demo.request.demo_instance_id)
    return store.read_run(run.id)


def now():
    return datetime.now(timezone.utc)


def read(run, native="SYNTHETIC-DEMO-1", version_id=None):
    return store.read_version(version_id or run.version_ids[0], "external_archive", native,
                              True, "current", now())


def test_demo_fetch_really_publishes_version_with_server_clock_and_no_history_proof(demo):
    run = perform(demo, "demo-real-version")
    assert run.state == "completed" and run.is_demo is True
    assert run.fetch_mode == "simulated" and run.demo_instance_id == demo.request.demo_instance_id
    assert len(run.version_ids) == 1 and len(run.input_sha256) == 64
    record = read(run)
    assert record.raw_content == original().raw_content
    assert record.is_demo is True and record.availability_proof.kind == "unverified"
    assert record.first_seen_at >= run.created_at
    assert record.first_seen_at.year >= 2026 and record.source_published_at.value == "2020-01-01"
    assert run.coverage["mode"] == "bounded_demo_query"
    assert store.list_records("external_archive", True, "current", now(), run_id=run.id).coverage.mode == "bounded_demo_query"
    assert run.coverage["new_version_count"] == 1 and run.coverage["requests"] == 0
    assert run.coverage["complete"] is False and run.coverage["demo_instance_id"] == run.demo_instance_id
    with pytest.raises(HistoryError):
        store.read_version(record.version_id, "external_archive", record.source_record_id,
                           True, "public_history_reconstruction", now())
    assert store.list_records("external_archive", False, "current", now()).items == []
    demo.forbidden.assert_not_called()


def test_demo_idempotency_and_same_input_reuse_version_without_rereading_source(demo):
    first = perform(demo, "demo-idempotent")
    demo.path.unlink()
    repeated, fresh = store.create_fetch(demo.request, "demo-idempotent")
    assert fresh is False and repeated.id == first.id and repeated.version_ids == first.version_ids
    changed = demo.request.model_copy(update={"query": "different"})
    with pytest.raises(HistoryError) as exc:
        store.create_fetch(changed, "demo-idempotent")
    assert exc.value.code == "idempotency_conflict"
    demo.path.write_text(json.dumps(demo.payload))
    same = perform(demo, "same-original-different-request")
    assert same.version_ids == first.version_ids and same.coverage["new_version_count"] == 0
    with demo.engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one() == 1


def test_retry_uses_frozen_original_after_source_file_changes_and_disappears(demo):
    queued, _ = store.create_fetch(demo.request, "demo-retry-frozen")
    store.job_input(queued.id, 1)
    store.fail_job(queued.id, 1, "synthetic_worker_failure", "Explicit synthetic failure")
    demo.path.unlink()
    with pytest.raises(HistoryError):
        store.retry_fetch(queued.id, 1, job_inactive=False)
    recovered = store.retry_fetch(queued.id, 1, job_inactive=True)
    assert recovered.attempt == 2
    flow.history_fetch_job(queued.id, 2, demo_instance_id=demo.request.demo_instance_id)
    completed = store.read_run(queued.id)
    assert read(completed).raw_content == original().raw_content
    assert completed.input_sha256 == queued.input_sha256
    assert completed.attempts[0]["state"] == "failed"
    with pytest.raises(HistoryError):
        store.finish_fetch(queued.id, 1, {})


def test_new_original_keeps_old_version_and_rechecks_current_permission(demo, monkeypatch):
    old = perform(demo, "demo-old-version")
    demo.payload["sources"]["external_archive"] = [original(summary="SYNTHETIC corrected report").model_dump()]
    demo.path.write_text(json.dumps(demo.payload))
    new = perform(demo, "demo-new-version")
    assert new.version_ids != old.version_ids
    assert read(old).raw_content == original().raw_content
    assert "corrected report" in read(new).clean_text
    assert store.list_records("external_archive", True, "current", now()).items[0].version_id == new.version_ids[0]
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", "[]")
    with pytest.raises(HistoryError) as exc:
        read(old)
    assert exc.value.code == "source_not_allowed"


def test_publish_transaction_failure_is_durable_and_keeps_old_version(demo, monkeypatch):
    old = perform(demo, "demo-stable-before-failure")
    demo.payload["sources"]["external_archive"] = [original(summary="SYNTHETIC should roll back").model_dump()]
    demo.path.write_text(json.dumps(demo.payload))
    queued, _ = store.create_fetch(demo.request, "demo-failed-publish")
    actual_publish = store._publish
    def fail_after_insert(*args, **kwargs):
        actual_publish(*args, **kwargs)
        raise HistoryError("synthetic_publish_failure", "Deliberate rollback after real insert", 503)
    monkeypatch.setattr(store, "_publish", fail_after_insert)
    with pytest.raises(HistoryError):
        flow.history_fetch_job(queued.id, 1, demo_instance_id=demo.request.demo_instance_id)
    failed = store.read_run(queued.id)
    assert failed.state == "failed" and failed.version_ids == []
    assert failed.error["code"] == "synthetic_publish_failure"
    assert read(old).raw_content == original().raw_content
    with demo.engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one() == 1


def test_unknown_success_result_preserves_committed_run_and_never_republishes(demo, monkeypatch):
    queued, _ = store.create_fetch(demo.request, "demo-unknown-commit")
    actual_finish = flow.finish_fetch
    def lose_response(*args, **kwargs):
        actual_finish(*args, **kwargs)
        raise RuntimeError("synthetic response loss after commit")
    monkeypatch.setattr(flow, "finish_fetch", lose_response)
    with pytest.raises(RuntimeError):
        flow.history_fetch_job(queued.id, 1, demo_instance_id=demo.request.demo_instance_id)
    committed = store.read_run(queued.id)
    assert committed.state == "completed" and len(committed.version_ids) == 1
    with pytest.raises(HistoryError) as exc:
        flow.history_fetch_job(queued.id, 1, demo_instance_id=demo.request.demo_instance_id)
    assert exc.value.code == "stale_attempt"
    assert store.read_run(queued.id).version_ids == committed.version_ids


def test_demo_query_zero_has_explicit_receipt_but_bad_package_is_not_success(demo):
    demo.request = demo.request.model_copy(update={"query": "absent-literal"})
    zero = perform(demo, "demo-zero-query")
    assert zero.version_ids == [] and zero.coverage["stop_reason"] == "demo_query_no_match"
    assert zero.coverage["adopted_count"] == zero.coverage["source_total"] == 0
    demo.path.write_text("invalid JSON")
    with pytest.raises(HistoryError):
        store.create_fetch(demo.request, "demo-bad-input")
    with demo.engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM history_runs")).scalar_one() == 1


def test_demo_bounded_partial_and_forged_worker_result_cannot_publish(demo):
    demo.payload["sources"]["external_archive"].append(original("SYNTHETIC-DEMO-2").model_dump())
    demo.path.write_text(json.dumps(demo.payload))
    demo.request = demo.request.model_copy(update={"max_records": 1})
    partial = perform(demo, "demo-real-partial")
    assert partial.state == "partial" and len(partial.version_ids) == 1
    assert partial.coverage["source_total"] == 2 and partial.coverage["stop_reason"] == "record_budget"
    queued, _ = store.create_fetch(demo.request, "demo-forged-result")
    request, frozen = store.job_input(queued.id, 1)
    forged = flow.read_demo_fetch(request, frozen)
    forged["records"] = [original(summary="forged replacement")]
    with pytest.raises(HistoryError) as exc:
        store.finish_fetch(queued.id, 1, forged)
    assert exc.value.code == "demo_input_invalid"
    assert store.read_run(queued.id).state == "running"
    assert read(partial).raw_content == original().raw_content
