"""Demo fetch must adopt frozen synthetic originals through existing History/RQ."""

import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import history as api
from biz_rules.history_models import ArchiveRecord, FetchRequest, HistoryError, RunView, canonical, sha256
from db import history as store
from flows import history_fetch as flow


INSTANCE = "r5-isolated-demo"


def original(native="DEMO-1", text="A concrete synthetic cluster report"):
    return ArchiveRecord(source_record_id=native, record_kind="document", raw_content=json.dumps({
        "record_kind": "document", "title": native, "summary": text,
        "source_published_at": "2020-01-01", "material_type": "social_media_post",
    }))


def request(**changes):
    body = dict(source_id="external_archive", query="*", is_demo=True, demo_instance_id=INSTANCE)
    body.update(changes)
    return FetchRequest(**body)


@pytest.fixture
def demo_config(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-source.json"
    payload = {"schema_version": "datafabric.demo_fetch.v1", "instance_id": INSTANCE,
               "sources": {"external_archive": [original().model_dump()]}}
    path.write_text(json.dumps(payload))
    config = {"instance_id": INSTANCE,
              "database": {"host": "127.0.0.1", "port": 19876, "name": "fabric_r5_demo"},
              "redis": {"host": "127.0.0.1", "port": 19877, "db": 0},
              "input_file": str(path)}
    monkeypatch.setenv("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG", json.dumps(config))
    monkeypatch.setenv("DATAFABRIC_HISTORY_DATABASE_URL", "postgresql+psycopg2://demo:secret@127.0.0.1:19876/fabric_r5_demo")
    monkeypatch.setenv("DATAFABRIC_HISTORY_REDIS_URL", "redis://127.0.0.1:19877/0")
    return path, payload, config


def test_demo_request_requires_instance_and_real_request_cannot_carry_it():
    assert request().is_demo is True
    with pytest.raises(ValueError):
        request(demo_instance_id=None)
    with pytest.raises(ValueError):
        request(is_demo=False)
    with pytest.raises(ValueError):
        FetchRequest(source_id="external_archive", query="*")
    public = FetchRequest(source_id="clinicaltrials", query="BCMA")
    assert public.is_demo is False and public.demo_instance_id is None


@pytest.mark.parametrize("setting", [None, "{}", "not-json"])
def test_demo_rejected_without_explicit_server_isolation(monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG", raising=False)
    else:
        monkeypatch.setenv("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG", setting)
    with pytest.raises(HistoryError) as exc:
        flow.require_demo_fetch(request())
    assert exc.value.code == "demo_instance_not_configured"


@pytest.mark.parametrize("change", ["instance", "database", "redis", "nonlocal"])
def test_demo_rejected_on_instance_or_backend_mismatch(demo_config, monkeypatch, change):
    if change == "instance":
        item = request(demo_instance_id="another-demo")
    else:
        item = request()
    if change == "database":
        monkeypatch.setenv("DATAFABRIC_HISTORY_DATABASE_URL", "postgresql+psycopg2://u:p@127.0.0.1:19876/production")
    elif change == "redis":
        monkeypatch.setenv("DATAFABRIC_HISTORY_REDIS_URL", "redis://127.0.0.1:6379/0")
    elif change == "nonlocal":
        path, payload, config = demo_config
        config["database"]["host"] = "production.example"
        monkeypatch.setenv("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG", json.dumps(config))
        monkeypatch.setenv("DATAFABRIC_HISTORY_DATABASE_URL", "postgresql+psycopg2://u:p@production.example:19876/fabric_r5_demo")
    with pytest.raises(HistoryError) as exc:
        flow.require_demo_fetch(item)
    assert exc.value.code == "demo_instance_mismatch"


def test_demo_source_is_frozen_and_matched_without_external_reads(demo_config, monkeypatch):
    monkeypatch.setattr(flow, "read_public", Mock(side_effect=AssertionError("external fetch forbidden")))
    frozen = flow.freeze_demo_fetch(request())
    path, payload, config = demo_config
    path.write_text("changed after acceptance")
    batch = flow.read_demo_fetch(request(), frozen)
    assert [item.source_record_id for item in batch["records"]] == ["DEMO-1"]
    assert batch["records"][0].raw_content == original().raw_content
    assert batch["demo_instance_id"] == INSTANCE
    assert batch["input_sha256"] == sha256(canonical(frozen["records"]))
    assert batch["requests"] == 0
    assert batch["complete"] is False
    assert batch["upstream_pagination_exhausted"] is True
    flow.read_public.assert_not_called()


def test_demo_query_and_id_scope_are_not_ignored(demo_config):
    frozen = flow.freeze_demo_fetch(request(query="concrete cluster", source_record_ids=["DEMO-1"]))
    assert len(frozen["records"]) == 1
    unmatched = flow.freeze_demo_fetch(request(query="not-present"))
    batch = flow.read_demo_fetch(request(query="not-present"), unmatched)
    assert batch["records"] == [] and batch["stop_reason"] == "demo_query_no_match"
    assert batch["source_total"] == 0
    with pytest.raises(HistoryError) as exc:
        flow.freeze_demo_fetch(request(source_record_ids=["absent"]))
    assert exc.value.code == "demo_no_records"


def test_demo_budget_reports_partial_and_does_not_claim_full_source(demo_config):
    path, payload, config = demo_config
    payload["sources"]["external_archive"].append(original("DEMO-2").model_dump())
    path.write_text(json.dumps(payload))
    frozen = flow.freeze_demo_fetch(request(max_records=1))
    batch = flow.read_demo_fetch(request(max_records=1), frozen)
    assert len(batch["records"]) == 1
    assert batch["stop_reason"] == "record_budget"
    assert batch["source_total"] == 2
    assert batch["upstream_pagination_exhausted"] is False
    assert batch["complete"] is False


@pytest.mark.parametrize("damage", ["instance", "empty", "duplicate", "raw", "sha"])
def test_demo_invalid_or_tampered_input_fails_closed(demo_config, damage):
    path, payload, config = demo_config
    if damage == "sha":
        frozen = flow.freeze_demo_fetch(request())
        frozen["records"][0]["raw_content"] += "tampered"
        with pytest.raises(HistoryError) as exc:
            flow.read_demo_fetch(request(), frozen)
        assert exc.value.code == "demo_input_invalid"
        return
    if damage == "instance":
        payload["instance_id"] = "other"
    elif damage == "empty":
        payload["sources"]["external_archive"] = []
    elif damage == "duplicate":
        payload["sources"]["external_archive"] *= 2
    else:
        payload["sources"]["external_archive"][0]["raw_content"] = "not JSON"
    path.write_text(json.dumps(payload))
    with pytest.raises(HistoryError):
        flow.freeze_demo_fetch(request())


def test_demo_worker_never_calls_public_reader(demo_config, monkeypatch):
    item = request()
    frozen = flow.freeze_demo_fetch(item)
    monkeypatch.setattr(flow, "job_input", lambda *args: (item, frozen))
    external = Mock(side_effect=AssertionError("external forbidden"))
    monkeypatch.setattr(flow, "read_public", external)
    finish = Mock(return_value=SimpleNamespace(id="run", state="completed"))
    monkeypatch.setattr(flow, "finish_fetch", finish)
    result = flow.history_fetch_job("run", 1)
    assert result["state"] == "completed"
    assert finish.call_args.args[2]["records"][0].source_record_id == "DEMO-1"
    external.assert_not_called()


def test_public_worker_preserves_public_reader(monkeypatch):
    item = FetchRequest(source_id="clinicaltrials", query="*")
    monkeypatch.setattr(flow, "job_input", lambda *args: (item, None))
    external = Mock(return_value={"records": []})
    monkeypatch.setattr(flow, "read_public", external)
    monkeypatch.setattr(flow, "finish_fetch", lambda *args: SimpleNamespace(id="run", state="completed"))
    flow.history_fetch_job("run", 1)
    external.assert_called_once_with(item)


def test_demo_worker_failure_is_durable_and_not_empty_success(demo_config, monkeypatch):
    item = request()
    frozen = flow.freeze_demo_fetch(item)
    frozen["records"] = []
    monkeypatch.setattr(flow, "job_input", lambda *args: (item, frozen))
    monkeypatch.setattr(flow, "read_public", Mock(side_effect=AssertionError("external forbidden")))
    finish = Mock(side_effect=AssertionError("must not publish empty demo"))
    monkeypatch.setattr(flow, "finish_fetch", finish)
    failure = Mock()
    monkeypatch.setattr(flow, "fail_job", failure)
    with pytest.raises(HistoryError):
        flow.history_fetch_job("run", 1)
    failure.assert_called_once()
    finish.assert_not_called()


def test_disabled_public_fetch_does_not_block_local_demo_or_enable_public(demo_config, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "false")
    flow.require_fetch_allowed(request())
    with pytest.raises(HistoryError) as exc:
        flow.require_fetch_allowed(FetchRequest(source_id="clinicaltrials", query="*"))
    assert exc.value.code == "public_fetch_disabled"


def test_api_demo_identity_is_checked_before_any_database_write(demo_config, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setenv("DATAFABRIC_HISTORY_ADMIN_TOKEN", "unit-demo-admin")
    creation = Mock(side_effect=AssertionError("must not write"))
    monkeypatch.setattr(store, "create_fetch", creation)
    with TestClient(app) as client:
        response = client.post("/api/history/fetch", headers={"Authorization": "Bearer unit-demo-admin", "Idempotency-Key": "demo-mismatch"},
                               json=request(demo_instance_id="other-demo").model_dump(mode="json"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "demo_instance_mismatch"
    creation.assert_not_called()


def test_demo_cannot_bypass_worker_by_calling_public_reader(demo_config, monkeypatch):
    from biz_rules import history_sources as sources
    external = Mock(side_effect=AssertionError("HTTP must not be constructed"))
    monkeypatch.setattr(sources.httpx, "Client", external)
    with pytest.raises(HistoryError) as exc:
        sources.read_public(request(source_id="clinicaltrials"))
    assert exc.value.code == "demo_public_fetch_forbidden"
    external.assert_not_called()


@pytest.mark.parametrize("damage", ["oversized", "too_many", "missing_database", "missing_redis"])
def test_demo_input_budget_and_missing_backend_fail_before_adoption(demo_config, monkeypatch, damage):
    path, payload, config = demo_config
    if damage == "oversized":
        path.write_bytes(b" " * (16 * 1024 * 1024 + 1))
    elif damage == "too_many":
        payload["sources"]["external_archive"] *= 5001
        path.write_text(json.dumps(payload))
    elif damage == "missing_database":
        monkeypatch.delenv("DATAFABRIC_HISTORY_DATABASE_URL")
    else:
        monkeypatch.delenv("DATAFABRIC_HISTORY_REDIS_URL")
    with pytest.raises(HistoryError):
        flow.freeze_demo_fetch(request())


def test_demo_rejects_time_windows_and_public_continuations():
    for changes in [{"updated_since": "2020-01-01T00:00:00Z"},
                    {"updated_until": "2020-01-01T00:00:00Z"},
                    {"query": " "}]:
        with pytest.raises(ValueError):
            request(**changes)


@pytest.mark.parametrize("override", ["host=production.example", "hostaddr=192.0.2.1", "port=5432", "dbname=production", "service=production"])
def test_demo_database_url_query_cannot_override_bound_backend(demo_config, monkeypatch, override):
    monkeypatch.setenv("DATAFABRIC_HISTORY_DATABASE_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:19876/fabric_r5_demo?" + override)
    with pytest.raises(HistoryError) as exc:
        flow.require_demo_fetch(request())
    assert exc.value.code == "demo_instance_mismatch"


def test_demo_worker_rechecks_backend_after_request_acceptance(demo_config, monkeypatch):
    item = request()
    frozen = flow.freeze_demo_fetch(item)
    monkeypatch.setattr(flow, "job_input", lambda *args: (item, frozen))
    monkeypatch.setenv("DATAFABRIC_HISTORY_REDIS_URL", "redis://127.0.0.1:6379/0")
    external = Mock(side_effect=AssertionError("must not contact external source"))
    monkeypatch.setattr(flow, "read_public", external)
    failed = Mock()
    monkeypatch.setattr(flow, "fail_job", failed)
    with pytest.raises(HistoryError) as exc:
        flow.history_fetch_job("run", 1)
    assert exc.value.code == "demo_instance_mismatch"
    assert failed.call_args.args[2] == "demo_instance_mismatch"
    external.assert_not_called()



def test_enqueued_demo_carries_instance_for_pre_database_worker_guard(demo_config, monkeypatch):
    run = SimpleNamespace(id="run", attempt=1, is_demo=True, demo_instance_id=INSTANCE)
    queue = SimpleNamespace(connection=object(), enqueue=Mock())
    monkeypatch.setattr(flow, "history_queue", lambda: queue)
    monkeypatch.setattr(flow, "run_job_id", lambda _: "fixed-job")
    monkeypatch.setattr(flow.Job, "fetch", Mock(side_effect=flow.NoSuchJobError))
    monkeypatch.setattr(flow, "read_run", lambda _: run)
    flow.enqueue_fetch(run)
    assert queue.enqueue.call_args.kwargs["demo_instance_id"] == INSTANCE


def test_enqueued_demo_rejects_wrong_backend_before_reading_or_writing_it(demo_config, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/production")
    lookup = Mock(side_effect=AssertionError("production database must not be accessed"))
    failure = Mock(side_effect=AssertionError("production database must not be updated"))
    monkeypatch.setattr(flow, "job_input", lookup)
    monkeypatch.setattr(flow, "fail_job", failure)
    with pytest.raises(HistoryError) as exc:
        flow.history_fetch_job("run", 1, demo_instance_id=INSTANCE)
    assert exc.value.code == "demo_instance_mismatch"
    lookup.assert_not_called()
    failure.assert_not_called()



def test_retry_rejects_backend_drift_before_run_or_queue_lookup(demo_config, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/production")
    read = Mock(side_effect=AssertionError("must not read unknown database"))
    queue = Mock(side_effect=AssertionError("must not read unknown queue"))
    monkeypatch.setattr(flow, "read_run", read)
    monkeypatch.setattr(flow, "history_queue", queue)
    with pytest.raises(HistoryError) as exc:
        flow.retry_inactive("run", 1)
    assert exc.value.code == "demo_instance_mismatch"
    read.assert_not_called()
    queue.assert_not_called()


def test_configured_demo_instance_still_blocks_public_retry_before_queue(demo_config, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "false")
    monkeypatch.setattr(flow, "read_run", lambda _: SimpleNamespace(is_demo=False, demo_instance_id=None))
    queue = Mock(side_effect=AssertionError("must not touch public task queue"))
    monkeypatch.setattr(flow, "history_queue", queue)
    with pytest.raises(HistoryError) as exc:
        flow.retry_inactive("public-run", 1)
    assert exc.value.code == "public_fetch_disabled"
    queue.assert_not_called()



def test_enqueued_public_preserves_legacy_two_argument_job_without_demo_kwarg(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "true")
    run = SimpleNamespace(id="public-run", attempt=1, is_demo=False, demo_instance_id=None)
    queue = SimpleNamespace(connection=object(), enqueue=Mock())
    monkeypatch.setattr(flow, "history_queue", lambda: queue)
    monkeypatch.setattr(flow, "run_job_id", lambda _: "fixed-public-job")
    monkeypatch.setattr(flow.Job, "fetch", Mock(side_effect=flow.NoSuchJobError))
    monkeypatch.setattr(flow, "read_run", lambda _: run)
    flow.enqueue_fetch(run)
    assert queue.enqueue.call_args.args == (flow.history_fetch_job, "public-run", 1)
    assert queue.enqueue.call_args.kwargs == {
        "job_id": "fixed-public-job", "job_timeout": 180,
        "result_ttl": 86400, "failure_ttl": 604800,
    }
