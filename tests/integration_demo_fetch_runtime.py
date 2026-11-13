"""Native loopback HTTP + real RQ processes on receipt-bound, disposable resources.

Opt-in only. Keeps adopted synthetic versions and all nonbody receipts; stops only
API/worker process groups started by this test. Never starts or removes containers.
"""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from redis import Redis
from rq.job import Job
from sqlalchemy.engine import make_url

from fetch.fetch_job import require_demo_fetch


def test_native_demo_http_rq_restart_and_frozen_recovery(tmp_path):
    receipt_path = os.environ.get("DATAFABRIC_DEMO_TEST_RESOURCE_RECEIPT")
    raw_config = os.environ.get("DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG")
    if not receipt_path or not raw_config:
        pytest.skip("Explicit creation receipt and isolated demo backend configuration required")
    ownership = json.loads(Path(receipt_path).read_text())
    config = json.loads(raw_config)
    assert ownership["created_by_this_run"] is True and ownership["existing_resources_used"] is False
    assert ownership["run_id"] == config["instance_id"]
    assert ownership["purpose"] == "isolated Fabric demo fetch contract regression; no real model/public crawl"
    resources = {item["role"]: item for item in ownership["resources"]}
    parsed = make_url(os.environ["DATAFABRIC_HISTORY_DATABASE_URL"])
    assert (parsed.host, parsed.port, parsed.database) == (
        resources["postgres"]["host"], resources["postgres"]["port"], ownership["database_name"])
    assert (config["redis"]["host"], config["redis"]["port"], config["redis"]["db"]) == (
        resources["redis"]["host"], resources["redis"]["port"], 0)
    require_demo_fetch(SimpleNamespace(is_demo=True, demo_instance_id=config["instance_id"]))
    repo = Path(__file__).resolve().parents[1]
    execution_id = uuid.uuid4().hex[:12]
    native = "SYNTHETIC-R5-" + execution_id
    config["input_file"] = str(tmp_path / "currently-visible-synthetic-source.private.json")
    environment = os.environ.copy()
    environment.update({"DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG": json.dumps(config),
                        "DATAFABRIC_HISTORY_FETCH_ENABLED": "false",
                        "DATAFABRIC_HISTORY_SOURCE_ALLOWLIST": '["external_archive"]'})
    processes, logs = [], []
    receipt = {"execution_id": execution_id, "instance_id": config["instance_id"],
               "resource_creation_receipt": receipt_path, "is_demo": True,
               "input_kind": "currently_visible_local_synthetic_package",
               "public_http_requests": 0, "real_model_calls": 0, "runs": [], "processes": []}
    output = Path(os.environ.get("DATAFABRIC_DEMO_RUNTIME_OUTPUT_DIR", str(tmp_path))) / (
        "demo-http-rq-" + execution_id + ".nonbody.json")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    receipt["http_host"], receipt["http_port"] = "127.0.0.1", port
    base_url = "http://127.0.0.1:" + str(port)
    admin = {"Authorization": "Bearer " + environment["DATAFABRIC_HISTORY_ADMIN_TOKEN"]}
    reader = {"Authorization": "Bearer " + environment["DATAFABRIC_HISTORY_READ_TOKEN"]}
    client = httpx.Client(base_url=base_url, timeout=3)
    redis = Redis.from_url(environment["DATAFABRIC_HISTORY_REDIS_URL"])
    original_text = None

    def visible(summary):
        nonlocal original_text
        raw = json.dumps({"record_kind": "document", "title": native, "summary": summary,
                          "source_published_at": "2020-01-01", "material_type": "social_media_post",
                          "source_reference": "fixture://r5-runtime/" + native})
        package = {"schema_version": "datafabric.demo_fetch.v1", "instance_id": config["instance_id"],
                   "sources": {"external_archive": [{"source_record_id": native,
                               "record_kind": "document", "raw_content": raw}]}}
        path = Path(config["input_file"])
        path.write_text(json.dumps(package))
        path.chmod(0o600)
        return raw

    def start(role, arguments, env=None):
        log = (tmp_path / (role + "-" + str(len(processes)) + ".private.log")).open("w")
        os.chmod(log.name, 0o600)
        process = subprocess.Popen([sys.executable, *arguments], cwd=repo, env=env or environment,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        logs.append(log)
        processes.append(process)
        receipt["processes"].append({"role": role, "pid": process.pid, "started_by_this_test": True})
        return process

    def stop(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        next(item for item in receipt["processes"] if item["pid"] == process.pid).update(
            stopped=True, exit_code=process.returncode)

    def wait_for(predicate, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                result = predicate()
                if result:
                    return result
            except (httpx.HTTPError, ConnectionError):
                pass
            time.sleep(0.1)
        raise AssertionError("Owned demo process did not reach the required state")

    def api_start():
        process = start("api", ["-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(port)])
        wait_for(lambda: client.get("/health").status_code == 200)
        return process

    def worker_start(env=None):
        code = ("import os; from redis import Redis; from rq import Worker; "
                "Worker(['extraction'],connection=Redis.from_url(os.environ['DATAFABRIC_HISTORY_REDIS_URL']),"
                "name='r5-demo-" + execution_id + "-' + str(os.getpid())).work(with_scheduler=False)")
        return start("worker", ["-c", code], env=env)

    body = {"source_id": "external_archive", "query": "*", "is_demo": True,
            "demo_instance_id": config["instance_id"], "max_records": 1, "max_pages": 1}

    def fetch(label):
        response = client.post("/api/history/fetch", headers={**admin, "Idempotency-Key": execution_id + "-" + label}, json=body)
        assert response.status_code == 200
        run = response.json()
        assert run["is_demo"] is True and run["demo_instance_id"] == config["instance_id"]
        assert run["fetch_mode"] == "simulated"
        return run

    def complete(run):
        def finished():
            response = client.get("/api/history/runs/" + run["id"], headers=reader)
            assert response.status_code == 200
            result = response.json()
            if result["state"] == "failed":
                raise AssertionError("Synthetic demo fetch unexpectedly failed")
            return result if result["state"] == "completed" else None
        result = wait_for(finished)
        assert len(result["version_ids"]) == 1
        assert result["coverage"]["mode"] == "bounded_demo_query"
        assert result["coverage"]["new_version_count"] == 1
        assert result["coverage"]["requests"] == 0 and result["coverage"]["complete"] is False
        receipt["runs"].append({key: result[key] for key in (
            "id", "attempt", "state", "is_demo", "fetch_mode", "demo_instance_id", "input_sha256", "version_ids")})
        return result

    def raw(run):
        params = {"source_id": "external_archive", "source_record_id": native, "is_demo": "true",
                  "mode": "current", "cutoff": datetime.now(timezone.utc).isoformat()}
        response = client.get("/api/history/versions/" + run["version_ids"][0] + "/raw", headers=reader, params=params)
        assert response.status_code == 200
        assert response.headers["X-Content-SHA256"] == hashlib.sha256(response.content).hexdigest()
        return response.text

    try:
        original_text = visible("SYNTHETIC currently visible original report")
        api_process = api_start()
        live = client.post("/api/history/fetch", headers={**admin, "Idempotency-Key": execution_id + "-public-disabled"},
                           json={"source_id": "clinicaltrials", "query": "*"})
        assert live.status_code == 403 and live.json()["detail"]["code"] == "public_fetch_disabled"
        first = fetch("initial")
        assert first["state"] == "queued" and first["version_ids"] == []
        active = client.post("/api/history/runs/" + first["id"] + "/retry", headers=admin, json={"expected_attempt": 1})
        assert active.status_code == 409
        visible("SYNTHETIC changed file must not change the accepted job")
        stop(api_process)
        api_process = api_start()
        assert fetch("initial")["id"] == first["id"]
        worker = worker_start()
        first = complete(first)
        assert raw(first) == original_text
        repeated = fetch("initial")
        assert repeated["id"] == first["id"] and repeated["version_ids"] == first["version_ids"]
        stop(worker)

        corrected_text = visible("SYNTHETIC subsequently visible correction")
        second = fetch("correction")
        stop(api_process)
        api_process = api_start()
        worker = worker_start()
        second = complete(second)
        assert second["version_ids"] != first["version_ids"] and raw(second) == corrected_text
        assert raw(first) == original_text
        stop(worker)

        recovery_text = visible("SYNTHETIC frozen input for explicit failed-worker recovery")
        recover = fetch("recovery")
        wrong_environment = environment.copy()
        wrong_config = dict(config)
        wrong_config["instance_id"] = "wrong-demo-instance"
        wrong_environment["DATAFABRIC_HISTORY_DEMO_FETCH_CONFIG"] = json.dumps(wrong_config)
        worker = worker_start(wrong_environment)
        job_id = "history-" + recover["id"] + "-1"
        wait_for(lambda: Job.fetch(job_id, connection=redis).get_status(refresh=True) == "failed")
        stop(worker)
        queued = client.get("/api/history/runs/" + recover["id"], headers=reader).json()
        assert queued["state"] == "queued" and queued["version_ids"] == []
        Path(config["input_file"]).unlink()
        response = client.post("/api/history/runs/" + recover["id"] + "/retry", headers=admin, json={"expected_attempt": 1})
        assert response.status_code == 200 and response.json()["attempt"] == 2
        worker = worker_start()
        recovered = complete(response.json())
        assert recovered["input_sha256"] == recover["input_sha256"] and raw(recovered) == recovery_text
        assert raw(first) == original_text
        assert fetch("initial")["id"] == first["id"]
        receipt.update(status="passed", native_record_id=native, api_restarts=2,
                       worker_backend_rejection_recorded_in_rq=True,
                       queued_attempt_not_misreported_as_success=True, frozen_retry_attempt=2,
                       old_version_retained=True, retained_version_count=3)
    except Exception as exc:
        receipt.update(status="failed", failure_type=type(exc).__name__)
        raise
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        client.close()
        redis.close()
        receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
        with output.open("x") as saved:
            os.chmod(output, 0o600)
            json.dump(receipt, saved, ensure_ascii=False, indent=2)
        print("demo_runtime_receipt=" + str(output))
