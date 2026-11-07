"""Slice S1 on real PostgreSQL: WHO DON case counts from a fixed clean artifact to checked measurement objects.

The model is a loopback OpenAI-compatible server in this process (no model, no network beyond 127.0.0.1); the
checker is the real BioCheck /v3 rules (checker v17) run from a BioCheck checkout through its own app (TestClient,
FakeLLM - no model), located by DATAFABRIC_TEST_BIOCHECK_SRC / DATAFABRIC_TEST_BIOCHECK_PYTHON; without them the
tests that need the real checker are reported as skipped.

Covered: the run -> windows -> readings -> check -> objects chain and its receipts; idempotent re-runs; a failed
window owed until an explicit retry; a spent budget that is no failure; permission revoked between the read and the
send (nothing sent) and during the call (not adopted); a checker change (objects not current until re-checked); a
clean-text upgrade (re-read, relocated, owed) and a producer upgrade; corrections, retractions and withdrawals with
the two history modes kept apart; identities only from a confirmed graph identity bound to the mention; the change
feed; anti-loop (a graph package is never read for measurements); two workers never take the same window.
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import llm_transport as lt
import pytest
from integration_history import pg, pg_engine  # noqa: F401  (fixtures)
from sqlalchemy import text

from biz_rules.history_models import ArchiveImport, ArchiveRecord, HistoryError, archive_manifest_hash
from db import clean_stage, measurement_identity, measurement_reads, measurements
from db import extraction_checks as checks
from db import history as store
from db import measure_runs as runs
from processing import clean_text, registry
from processing import measure_extract as mx

SRC = os.environ.get("DATAFABRIC_TEST_BIOCHECK_SRC")
PYTHON = os.environ.get("DATAFABRIC_TEST_BIOCHECK_PYTHON") or sys.executable
real_checker = pytest.mark.skipif(not SRC, reason="DATAFABRIC_TEST_BIOCHECK_SRC (a BioCheck checkout) is not set")
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731  (as the other suites)
WHO_ID = "8c6e4a3e-2b0e-4b8e-9d55-1d1b1c7f5a01"
OTHER_ID = "8c6e4a3e-2b0e-4b8e-9d55-1d1b1c7f5a02"
FILLER = "".join(f"<p>第{n}次更新：应对小组继续开展接触者追踪、感染预防和社区参与工作，实验室检测能力已得到加强。</p>"
                 for n in "一二三四五六七八九十" * 4)
SENTENCE = "截至2026年10月28日，卢旺达卫生部在卢旺达全国累计共报告马尔堡病毒病{what}{n}例"


def who_raw(*, upgraded=False, uid=WHO_ID, title="马尔堡病毒病 - 卢旺达"):
    cases, deaths, recovered = (SENTENCE.format(what=w, n=n) for w, n in (("确诊病例", 66), ("死亡病例", 15),
                                                                          ("治愈病例", 40)))
    overview = (f"<h2>按报告日统计</h2><p>{cases}。</p>" + FILLER + f"<p>{deaths}。</p><p>{recovered}。</p>")
    if upgraded:  # what a later cleaner might give: one more paragraph first, and the deaths sentence twice
        overview = "<p>本期为更新后的疫情通报。</p>" + overview + f"<p>{deaths}。</p>"
    return {"Id": uid, "Title": title, "Overview": overview}


def measure(what, n, **extra):
    m = {"object_text": "卢旺达卫生部", "metric_text": f"报告马尔堡病毒病{what}", "value_text": str(n), "unit_text": "例",
         "period_text": "截至2026年10月28日", "period": "2026-10-28", "period_type": "point",
         "scope_text": "在卢旺达全国", "kind": "cumulative", "family": "case_count",
         "quote": SENTENCE.format(what=what, n=n), "dims": [{"kind": "disease", "text": "马尔堡病毒病"}]}
    m.update(extra)
    return m


CASES = measure("确诊病例", 66, dims=[{"kind": "disease", "text": "马尔堡病毒病"},
                                     {"kind": "date_basis", "text": "按报告日统计", "source": "window"}])
DEATHS = measure("死亡病例", 15)
RECOVERED = measure("治愈病例", 40, dims=[])
MEASURES = [CASES, DEATHS, RECOVERED]


# ---- the loopback model ------------------------------------------------------------------------------------------


class FakeModel:
    """POST /v1/chat/completions on 127.0.0.1: each window is answered with the scripted measures whose quote is in
    the window text. `script` holds per-request overrides, used in order: ("status", n), "timeout", "garbage", or a
    callable run before the default answer (a side effect such as a revocation)."""

    def __init__(self, measures=MEASURES):
        self.measures, self.requests, self.script = list(measures), [], []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                action = fake.script.pop(0) if fake.script else None
                status, payload = fake.answer(body, action)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def answer(self, body, action):
        if action == "timeout":
            time.sleep(1.5)
        if isinstance(action, tuple):
            return action[1], {"error": {"message": "synthetic"}}
        if callable(action):
            action(body)
        window = json.loads(body["messages"][1]["content"])["windows"][0]["text"]
        content = "not json" if action == "garbage" else json.dumps(
            {"items": [{"index": 1, "measures": [m for m in self.measures if m["quote"] in window]}]})
        return 200, {"id": "fake", "object": "chat.completion", "model": body["model"],
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}

    def model(self, name="fake-measure-model"):
        provider = lt.Provider(kind="openai_compatible", base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
                               model=name, api_key="synthetic-key", timeout_seconds=1.0, endpoint_id="loopback-fake")
        return runs.Model(provider, mx.ModelConfig(kind="openai_compatible", model=name, endpoint_id="loopback-fake",
                                                   thinking=False, temperature=0.0, max_tokens=2000))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ---- the real checker -------------------------------------------------------------------------------------------

DRIVER = """
import json, os, sys
os.environ["SERVICE_TOKEN"] = "test-token"
from fastapi.testclient import TestClient
from app.service import create_app
from conftest import FakeLLM
with TestClient(create_app(FakeLLM()), raise_server_exceptions=False) as client:
    r = client.post("/v1/extraction-consistency", json=json.load(sys.stdin), headers={"Authorization": "Bearer test-token"})
    print(json.dumps({"status": r.status_code, "body": r.json()}, ensure_ascii=False))
"""


class RealBioCheck:
    """The real BioCheck rules behind Fabric's client; answer(request, response) may alter what Fabric receives
    (a simulated checker of another version, or a faulty one)."""

    def __init__(self, answer=None):
        self.requests, self.answer = [], answer

    def handler(self, request):
        assert request.headers["Authorization"] == "Bearer test-token"
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        environ = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.path.join(SRC, "tests"))
        p = subprocess.run([PYTHON, "-c", DRIVER.replace("/v1/", request.url.path[:4])], input=json.dumps(body),
                           capture_output=True, text=True, env=environ, cwd=SRC, timeout=60, check=False)
        assert p.returncode == 0, p.stderr[-2000:]
        result = json.loads(p.stdout)
        if self.answer:
            self.answer(body, result["body"])
        return httpx.Response(result["status"], json=result["body"])

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def as_checker(version):
    def answer(_request, response):
        response["checker_version"] = version
    return answer


# ---- fixtures and helpers -------------------------------------------------------------------------------------

MEASURE_TABLES = ("derived_measure_correction_resolutions,derived_measure_correction_claims,"
                  "derived_measure_correction_links,derived_measure_correction_cursor,derived_measurement_identities,"
                  "derived_measurement_events,derived_measurement_feed,derived_measurements,derived_measure_relocations,"
                  "derived_measure_readings,derived_measure_requests,derived_measure_window_attempts,"
                  "derived_measure_windows,derived_measure_runs,derived_extraction_checks,derived_extraction_check_tasks")


@pytest.fixture
def db(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST",
                       '["who_don","external_archive","reviewed_graph","clinicaltrials"]')
    monkeypatch.setenv("DATAFABRIC_BIOCHECK_URL", "http://biocheck.invalid")
    monkeypatch.setenv("DATAFABRIC_BIOCHECK_TOKEN", "test-token")
    monkeypatch.delenv("DATAFABRIC_MEASURE_CHECKER", raising=False)
    with pg.begin() as conn:
        conn.execute(text("TRUNCATE " + MEASURE_TABLES))
    return pg


@pytest.fixture
def fake():
    model = FakeModel()
    yield model
    model.close()


def rows(pg, sql, **params):
    with pg.begin() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def who_version(raw=None, key="who-a", *, available="2026-10-29", source_id="who_don"):
    """A fixed WHO DON version (an archive with proof of the day it was public) and its clean artifact."""
    raw = raw or who_raw()
    records = [ArchiveRecord(source_record_id=raw["Id"], record_kind="document",
                             raw_content=json.dumps(raw, ensure_ascii=False))]
    proof = {"available_at": {"value": available, "precision": "day"}, "uri": "fixture://who-don-archive",
             "sha256": archive_manifest_hash(records), "reviewed": True, "review_note": "synthetic WHO DON fixture"}
    run = store.import_archive(ArchiveImport(source_id=source_id, is_demo=True, scope_description="synthetic WHO DON",
                                             declared_count=1, records=records, proof=proof), key)
    clean_stage.process(time_budget_seconds=60)
    return run.version_ids[0]


def read_all(fake, model=None, budget=None):
    states = []
    for _ in range(100):
        state = runs.process_once(model=model or fake.model(), budget=budget)["state"]
        if state == "idle":
            return states
        states.append(state)
    raise AssertionError("windows never ran out")


def check_all(bio):
    """Every due measures check task (the identifiers / profile tasks of the clean stage are not this slice's)."""
    with store.history_engine().begin() as conn:
        conn.execute(text("DELETE FROM derived_extraction_check_tasks WHERE producer<>'measures'"))
    states = []
    for _ in range(100):
        state = checks.process_once(client=bio.client())["state"]
        if state == "idle":
            return states
        states.append(state)
    raise AssertionError("check tasks never ran out")


def listing(version, mode="current", cutoff=None, source_id="who_don", **kw):
    return measurement_reads.for_versions(source_id, True, [version], mode, cutoff or NOW(), **kw)


def chain(fake, version=None):
    """Plan, read every window and check every reading with the real checker: (version, the current receipts)."""
    version = version or who_version()
    assert runs.plan(model=fake.model()) >= 0
    read_all(fake)
    check_all(RealBioCheck())
    return version, listing(version)["measurements"]


def due(pg, only=None):
    """Windows due now (only: just that window index due, the others a day later)."""
    with pg.begin() as conn:
        conn.execute(text("UPDATE derived_measure_windows SET next_at=CASE WHEN CAST(:i AS integer) IS NULL "
                          "OR window_index=:i THEN clock_timestamp() ELSE clock_timestamp()+interval '1 day' END"),
                     {"i": only})


def events_of(pg, mid):
    return [r["kind"] for r in rows(pg, "SELECT kind FROM derived_measurement_events WHERE measurement_id=:m "
                                        "ORDER BY event_id", m=mid)]


def by_value(items):
    return {i["value_text"]: i for i in items}


def revoke(pg, source="who_don", enabled=False):
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=:e WHERE source_id=:s"), {"e": enabled, "s": source})


# ---- the chain ------------------------------------------------------------------------------------------------


@real_checker
def test_who_case_counts_become_checked_objects_with_their_receipts(db, fake):
    version, items = chain(fake)
    got = by_value(items)
    assert set(got) == {"66", "15", "40"}
    cases = got["66"]
    assert cases["contract_version"] == "datafabric.data_objects.v2" and cases["kind"] == "measurement"
    assert cases["object"] == "卢旺达卫生部" and cases["metric"] == "报告马尔堡病毒病确诊病例" and cases["unit"] == "例"
    assert cases["period"] == {"type": "point", "precision": "day", "value": "2026-10-28", "start": None, "end": None}
    assert cases["scope"] == "在卢旺达全国" and cases["scope_mode"] == "stated"
    assert cases["denominator_state"] == "not_applicable" and cases["measure_kind"] == "cumulative"
    # the measurement basis: each stated dimension with the checker's own result
    assert {d["kind"]: d["check"] for d in cases["dims"]} == {"disease": "match", "date_basis": "match"}
    assert cases["check"]["checker_version"] == "biocheck.extraction_rules.v17"
    assert cases["check"]["contract_version"] == "extraction_consistency.v3" and cases["check"]["status"] == "consistent"
    assert cases["inputs"][0]["version_id"] == version and cases["permission_sources"] == ["who_don"]
    assert cases["state_at_cutoff"] == "ok" and cases["current"] is True and cases["reconstruction"] is False
    assert cases["versions"]["producer"].startswith("datafabric.measure_extract.v3+")
    assert cases["identity"]["state"] == "provisional" and cases["identity"]["key"].startswith("literal:who_don|")
    (clean,) = rows(db, "SELECT text,text_sha256 FROM derived_clean_text WHERE version_id=:v", v=version)
    loc = cases["locators"][0]
    assert clean["text"][loc["start"]:loc["end"]] == cases["quote"] and loc["derived_text_sha256"] == clean["text_sha256"]
    # every model request was registered before it was sent and settled once, with the raw response
    reqs = rows(db, "SELECT delivered,http_status,response_text,settled_at FROM derived_measure_requests")
    assert len(reqs) == len(fake.requests) and all(r["delivered"] == "yes" and r["settled_at"] for r in reqs)
    assert all(json.loads(r["response_text"])["choices"] for r in reqs)
    attempts = rows(db, "SELECT outcome,requests,output FROM derived_measure_window_attempts")
    assert {a["outcome"] for a in attempts} == {"sent"} and all(a["requests"] == 1 and a["output"] for a in attempts)
    cov = runs.coverage("who_don", True, [version], "current", NOW())["versions"][version]
    assert cov["state"] == "complete" and cov["owed"] == [] and cov["read"] == cov["windows_total"] > 1
    # the checks were answered under contract v3, one strict request per reading
    results = rows(db, "SELECT contract_version,checker_version,status FROM derived_extraction_checks")
    assert len(results) == 3 and {(r["contract_version"], r["checker_version"], r["status"]) for r in results} == {
        ("extraction_consistency.v3", "biocheck.extraction_rules.v17", "completed")}
    tasks = rows(db, "SELECT producer,state,checker_version FROM derived_extraction_check_tasks")
    assert tasks == [{"producer": "measures", "state": "done", "checker_version": "biocheck.extraction_rules.v17"}]


@real_checker
def test_a_rerun_changes_nothing(db, fake):
    version, items = chain(fake)
    before = {t: rows(db, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in (
        "derived_measure_runs", "derived_measure_readings", "derived_measurements", "derived_measurement_events")}
    sent = len(fake.requests)
    assert runs.plan(model=fake.model()) == 0  # the same original, artifact and producer: no new run
    assert read_all(fake) == [] and len(fake.requests) == sent
    with db.begin() as conn:  # the check run again: every request answered before is reused
        conn.execute(text("UPDATE derived_extraction_check_tasks SET state='queued',next_check_at=clock_timestamp()"))
    bio = RealBioCheck()
    check_all(bio)
    assert bio.requests == []
    after = {t: rows(db, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in before}
    assert after == before
    assert [i["measurement_id"] for i in listing(version)["measurements"]] == [i["measurement_id"] for i in items]


# ---- debt, budget, permission ------------------------------------------------------------------------------


def test_a_failed_window_is_owed_until_it_is_explicitly_retried(db, fake):
    version = who_version()
    assert runs.plan(model=fake.model()) == 1
    total = rows(db, "SELECT windows_total FROM derived_measure_runs")[0]["windows_total"]
    fake.script = ["timeout", ("status", 500), "garbage"]  # three failures of window 0
    for _ in range(3):
        due(db, only=0)
        runs.process_once(model=fake.model())
    (w,) = rows(db, "SELECT state,attempts,reason FROM derived_measure_windows WHERE state<>'pending'")
    assert (w["state"], w["attempts"]) == ("failed_final", 3)
    due(db)
    read_all(fake)
    cov = runs.coverage("who_don", True, [version], "current", NOW())["versions"][version]
    assert cov["state"] == "partial" and cov["read"] == total - 1
    assert [o["state"] for o in cov["owed"]] == ["failed_final"]
    outcomes = [a["outcome"] for a in rows(db, "SELECT outcome FROM derived_measure_window_attempts ORDER BY id")]
    assert outcomes[:3] == ["failed", "failed", "invalid_output"]
    # a timeout is an unknown outcome: settled as such, never resent within the call
    assert rows(db, "SELECT delivered,error FROM derived_measure_requests ORDER BY registered_at LIMIT 1")[0] == {
        "delivered": "unknown", "error": "timeout"}
    assert runs.retry_failed(source_id="who_don") == 1
    read_all(fake)
    cov = runs.coverage("who_don", True, [version], "current", NOW())["versions"][version]
    assert cov["state"] == "complete" and cov["owed"] == []
    assert rows(db, "SELECT state FROM derived_extraction_check_tasks WHERE producer='measures'")[0]["state"] == "queued"


def test_a_spent_budget_is_no_failure(db, fake):
    version = who_version()
    runs.plan(model=fake.model())
    budget = runs.RequestBudget(0)
    assert runs.process_once(model=fake.model(), budget=budget)["state"] == "budget_spent"
    assert fake.requests == []  # nothing was sent
    fake.script = [("status", 429)]
    assert runs.process_once(model=fake.model())["state"] == "budget_spent"  # the provider said "not now"
    ws = rows(db, "SELECT state,attempts FROM derived_measure_windows WHERE state='budget_spent'")
    assert ws and all(w["attempts"] == 0 for w in ws)
    assert rows(db, "SELECT state FROM derived_measure_runs")[0]["state"] == "reading"
    due(db)
    read_all(fake)  # the next round reads on
    assert runs.coverage("who_don", True, [version], "current", NOW())["versions"][version]["state"] == "complete"


def test_a_revocation_between_read_and_send_sends_nothing(db, fake, monkeypatch):
    version = who_version()
    runs.plan(model=fake.model())
    payload = mx.window_payload

    def revoked_after_the_read(*args):  # the window was read and checked readable; then the source is revoked
        revoke(db)
        return payload(*args)

    monkeypatch.setattr(mx, "window_payload", revoked_after_the_read)
    assert runs.process_once(model=fake.model())["state"] == "source_unreadable"
    assert fake.requests == [] and rows(db, "SELECT * FROM derived_measure_requests") == []
    assert rows(db, "SELECT state FROM derived_measure_runs")[0]["state"] == "paused_unreadable"
    monkeypatch.setattr(mx, "window_payload", payload)
    assert runs.process_once(model=fake.model())["state"] == "idle"  # a paused run is not read
    assert runs.resume() == 0  # still revoked
    revoke(db, enabled=True)
    assert runs.resume() == 1
    # revoked while the call runs: the answer is kept as the request's raw outcome but not adopted
    fake.script = [lambda body: revoke(db)]
    assert runs.process_once(model=fake.model())["state"] == "source_unreadable"
    assert rows(db, "SELECT count(*) AS n FROM derived_measure_readings")[0]["n"] == 0
    (req,) = rows(db, "SELECT delivered,response_text FROM derived_measure_requests")
    assert req["delivered"] == "yes" and req["response_text"]
    with pytest.raises(HistoryError) as error:  # and nothing is read from a revoked source
        listing(version)
    assert error.value.status == 403
    revoke(db, enabled=True)
    runs.resume()
    read_all(fake)
    assert runs.coverage("who_don", True, [version], "current", NOW())["versions"][version]["state"] == "complete"


# ---- checker, processor and producer upgrades -----------------------------------------------------------------


@real_checker
def test_a_checker_change_leaves_objects_not_current_until_rechecked(db, fake, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_MEASURE_CHECKER", "biocheck.extraction_rules.v15")
    version = who_version()
    runs.plan(model=fake.model())
    read_all(fake)
    check_all(RealBioCheck(answer=as_checker("biocheck.extraction_rules.v15")))
    old = by_value(listing(version)["measurements"])["66"]
    assert old["check"]["checker_version"] == "biocheck.extraction_rules.v15"
    monkeypatch.delenv("DATAFABRIC_MEASURE_CHECKER")  # v17 is expected now
    now = listing(version)
    assert now["measurements"] == [] and now["excluded"] == {"checker_not_current": 3}
    one = measurement_reads.read_one(old["measurement_id"], True, "current", NOW())
    assert one["current"] is False and one["not_current_reason"] == "checker_not_current"
    assert events_of(db, old["measurement_id"]) == ["created"]  # nothing deleted, nothing rewritten
    assert checks.backfill_measures() == 1
    check_all(RealBioCheck())
    new = by_value(listing(version)["measurements"])["66"]
    assert new["check"]["checker_version"] == "biocheck.extraction_rules.v17"
    assert new["measurement_id"] != old["measurement_id"] and new["reading_id"] == old["reading_id"]
    assert events_of(db, old["measurement_id"]) == ["created", "rechecked_by_checker"]
    again = measurement_reads.read_one(old["measurement_id"], True, "current", NOW())
    assert again["state_at_cutoff"] == "superseded" and again["status"]["superseded_by"] == new["measurement_id"]


@real_checker
def test_a_clean_text_upgrade_rereads_relocates_and_owes(db, fake, monkeypatch):
    version, items = chain(fake)
    old = by_value(items)
    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "datafabric.clean_text.v13")
    real = clean_text.clean_who
    monkeypatch.setattr(clean_text, "clean_who", lambda raw: real(json.dumps(who_raw(upgraded=True),
                                                                               ensure_ascii=False)))
    clean_stage.process(time_budget_seconds=60)
    fake.measures = [CASES]  # the new reading finds only the case count again
    assert runs.plan(model=fake.model()) == 1
    read_all(fake)
    reloc = {r["outcome"]: r for r in rows(db, "SELECT outcome,reason,quote_occurrences FROM derived_measure_relocations")}
    assert set(reloc) == {"reread", "relocated", "owed"}
    assert reloc["owed"]["reason"] == "quote_in_several_places" and reloc["owed"]["quote_occurrences"] == 2
    sent = len(fake.requests)
    check_all(RealBioCheck())
    assert len(fake.requests) == sent  # a relocated reading is checked without a model call
    assert events_of(db, old["66"]["measurement_id"]) == ["created", "superseded_by_processor"]
    assert events_of(db, old["40"]["measurement_id"]) == ["created", "superseded_by_processor"]
    assert events_of(db, old["15"]["measurement_id"]) == ["created"]  # owed: the old object stays
    current = by_value(listing(version)["measurements"])
    assert current["66"]["artifact"]["text_processor_version"] == "datafabric.clean_text.v13"
    assert current["15"]["artifact"]["text_processor_version"] == "datafabric.clean_text.v12"
    # replay before the upgrade's results existed still shows the old object as it was
    at = rows(db, "SELECT min(produced_at) AS t FROM derived_measurements WHERE text_processor_version=:t",
              t="datafabric.clean_text.v13")
    before = at[0]["t"] - timedelta(microseconds=1)
    replay = by_value(listing(version, "operational_replay", before)["measurements"])
    assert replay["66"]["measurement_id"] == old["66"]["measurement_id"] and replay["66"]["state_at_cutoff"] == "ok"
    assert replay["66"]["events_after_cutoff"] == 1


@real_checker
def test_a_producer_upgrade_is_planned_only_when_asked_and_supersedes_by_producer(db, fake):
    version, items = chain(fake)
    newer = fake.model("fake-measure-model-2")
    assert runs.plan(model=newer) == 0  # another model is not an automatic re-read of history
    assert runs.plan(model=newer, producer_upgrade=True) == 1
    read_all(fake, model=newer)
    check_all(RealBioCheck())
    old = by_value(items)["66"]
    assert events_of(db, old["measurement_id"]) == ["created", "superseded_by_producer"]
    new = by_value(listing(version)["measurements"])["66"]
    assert new["versions"]["producer"] != old["versions"]["producer"]
    assert new["processing"]["config_sha256"] != old["processing"]["config_sha256"]


# ---- corrections and the two history modes ----------------------------------------------------------------------


def correction(target, key, relation="corrects", record="SYNTHETIC-correction", available="2026-11-02", relations=True):
    raw = {"record_kind": "document", "title": "更正通报", "full_text": "前述通报中的病例数更正为65例。",
           "material_kind": "official_notice"}
    if relations:
        raw["document_relations"] = [{"relation_type": relation, "target_source_id": "who_don",
                                      "target_source_record_id": WHO_ID, "target_version_id": target}]
    records = [ArchiveRecord(source_record_id=record, record_kind="document", raw_content=json.dumps(raw, ensure_ascii=False))]
    proof = {"available_at": {"value": available, "precision": "day"}, "uri": "fixture://correction",
             "sha256": archive_manifest_hash(records), "reviewed": True, "review_note": "synthetic correction"}
    return store.import_archive(ArchiveImport(source_id="external_archive", is_demo=True, scope_description="corr",
                                              declared_count=1, records=records, proof=proof), key).version_ids[0]


def scan():
    with store.history_engine().begin() as conn:
        return measurements.scan_corrections(conn, ["external_archive", "who_don"])


@real_checker
def test_corrections_retractions_and_withdrawals_follow_their_visibility(db, fake):
    version, items = chain(fake)
    cases = by_value(items)["66"]
    before = NOW()
    corr = correction(version, "corr-1")
    assert scan() == {version}
    assert scan() == set()  # the cursor moved on: nothing scanned twice
    now = measurement_reads.read_one(cases["measurement_id"], True, "current", NOW())
    assert now["state_at_cutoff"] == "corrected" and now["status"]["reason"] == "corrected_by_source"
    (ev,) = rows(db, "SELECT * FROM derived_measurement_events WHERE kind='corrected' AND measurement_id=:m",
                 m=cases["measurement_id"])
    (seen,) = rows(db, "SELECT first_seen_at,proof_available_at FROM history_versions WHERE version_id=:v", v=corr)
    # three times: the correcting material's visibility, Fabric's result, and the state's effect (= the material's)
    assert ev["material_visible_at"] == seen["first_seen_at"] and ev["effective_at"] == ev["material_visible_at"]
    assert ev["material_proof_at"] == seen["proof_available_at"] and ev["produced_at"] >= ev["material_visible_at"]
    assert ev["cause"]["version_id"] == corr and ev["cause_source_id"] == "external_archive"
    replay = measurement_reads.read_one(cases["measurement_id"], True, "operational_replay", before)
    assert replay["state_at_cutoff"] == "ok" and replay["events_after_cutoff"] == 1
    # the correcting source revoked: the correction is withheld, never shown
    revoke(db, "external_archive")
    hidden = measurement_reads.read_one(cases["measurement_id"], True, "current", NOW())
    assert hidden["state_at_cutoff"] == "ok" and hidden["events_withheld"] == 1
    revoke(db, "external_archive", enabled=True)
    # a later version of the correcting record no longer states it: withdrawn
    correction(version, "corr-2", relations=False, available="2026-11-04")
    scan()
    assert measurement_reads.read_one(cases["measurement_id"], True, "current", NOW())["state_at_cutoff"] == "ok"
    correction(version, "retract-1", relation="retracts", record="SYNTHETIC-retraction")
    scan()
    assert measurement_reads.read_one(cases["measurement_id"], True, "current", NOW())["state_at_cutoff"] == "retracted"
    kinds = events_of(db, cases["measurement_id"])
    assert kinds == ["created", "corrected", "correction_withdrawn", "retracted"]
    # a version measured after its correction arrived gets the correction too (both arrival orders)
    other = who_version(who_raw(uid=OTHER_ID), "who-b")
    corr_b = correction(other, "corr-b", record="SYNTHETIC-correction-b")
    scan()
    _, later = chain(fake, other)
    first = by_value(later)["66"]
    assert first["state_at_cutoff"] == "corrected" and corr_b


@real_checker
def test_public_history_reconstruction_is_kept_apart_from_operational_replay(db, fake):
    version, items = chain(fake)
    cases = by_value(items)["66"]
    past = datetime(2026, 10, 31, tzinfo=timezone.utc)  # after the archive's proof day, before anything was imported
    assert listing(version, "operational_replay", past)["measurements"] == []  # the system had produced nothing then
    with pytest.raises(HistoryError) as error:
        measurement_reads.read_one(cases["measurement_id"], True, "operational_replay", past)
    assert error.value.code == "version_not_found"
    rebuilt = listing(version, "public_history_reconstruction", past)
    assert rebuilt["reconstruction"] is True
    assert rebuilt["versions_used"]["checkers"] == ["biocheck.extraction_rules.v17"]
    assert rebuilt["versions_used"]["producers"] == [cases["versions"]["producer"]]
    got = by_value(rebuilt["measurements"])["66"]
    assert got["reconstruction"] is True and got["state_at_cutoff"] == "ok"
    correction(version, "corr-proof", available="2026-11-02")
    scan()
    # reconstruction applies a correction from its own proof of publication, not from when Fabric saw it
    assert by_value(listing(version, "public_history_reconstruction", past)["measurements"])["66"]["state_at_cutoff"] == "ok"
    later = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert by_value(listing(version, "public_history_reconstruction", later)["measurements"])["66"][
        "state_at_cutoff"] == "corrected"


# ---- identity, feed, anti-loop, adoption, concurrency ---------------------------------------------------------


def graph_package(version, node_status="confirmed", span=None, key="graph-a"):
    from biz_rules.reviewed_graph import GraphBundle

    (v,) = rows(store.history_engine(), "SELECT payload->>'raw_sha256' AS raw FROM history_versions WHERE version_id=:v",
                v=version)
    (clean,) = rows(store.history_engine(), "SELECT text,text_sha256,processor_version FROM derived_clean_text "
                                            "WHERE version_id=:v", v=version)
    start = clean["text"].index("卢旺达卫生部") if span is None else span[0]
    end = start + len("卢旺达卫生部") if span is None else span[1]
    ref = {"source_id": "who_don", "source_record_id": WHO_ID, "version_id": version, "raw_sha256": v["raw"]}
    node = {"namespace": "synthetic.org", "id": "rwanda-moh", "entity_type": "Organization", "name": "卢旺达卫生部",
            "identity_status": node_status, "review": {"status": "approved", "reviewed_by": "tang.weiwen",
                                                       "reviewed_at": "2026-11-01T00:00:00Z"},
            "identity_review_basis": "人工核对：卫生部官方名称" if node_status == "confirmed" else None,
            "evidence": [{**ref, "quote": clean["text"][start:end],
                          "locator": {"basis": "derived_clean_text", "start": start, "end": end,
                                      "text_processor_version": clean["processor_version"],
                                      "text_sha256": clean["text_sha256"]}}]}
    bundle = GraphBundle.model_validate({"graph_kind": "reviewed", "namespace": "synthetic.reviewed", "is_demo": True,
                                         "title": "SYNTHETIC reviewed identities", "origin": "explicit_demo",
                                         "scope_description": "SYNTHETIC", "dependency_source_ids": ["who_don"],
                                         "document_refs": [ref], "nodes": [node], "edges": []})
    run = store.import_archive(ArchiveImport(source_id="reviewed_graph", is_demo=True, scope_description="SYNTHETIC",
                                             declared_count=1, records=[ArchiveRecord(
                                                 source_record_id="bundle-" + key, record_kind="graph",
                                                 raw_content=bundle.model_dump_json())]), key)
    return run.version_ids[0], "bundle-" + key


@real_checker
def test_identity_only_from_a_confirmed_graph_identity_bound_to_the_mention(db, fake):
    version, items = chain(fake)
    _, other = chain(fake, who_version(who_raw(uid=OTHER_ID), "who-b"))
    cases, same_name = by_value(items)["66"], by_value(other)["66"]
    # the same name in another record is another, record-local key: never merged on the name
    assert cases["object"] == same_name["object"] and cases["identity"]["key"] != same_name["identity"]["key"]
    graph, record = graph_package(version)
    bind = {"graph_source_id": "reviewed_graph", "graph_record_id": record, "graph_version_id": graph,
            "namespace": "synthetic.org", "identity_id": "rwanda-moh"}
    with pytest.raises(HistoryError) as error:  # the identity's evidence is the first record's mention, not this one
        measurement_identity.bind(same_name["measurement_id"], **bind)
    assert error.value.code == "identity_not_bound_to_mention"
    measurement_identity.bind(cases["measurement_id"], **bind)
    got = by_value(listing(version)["measurements"])["66"]
    assert got["identity"] == {"state": "confirmed", "key": "synthetic.org|rwanda-moh",
                               "graph": {"source_id": "reviewed_graph", "record_id": record, "version_id": graph}}
    assert by_value(listing(other[0]["inputs"][0]["version_id"])["measurements"])["66"]["identity"]["state"] == \
        "provisional"
    unconfirmed, record2 = graph_package(version, node_status="unresolved", key="graph-b")
    with pytest.raises(HistoryError) as error:
        measurement_identity.bind(cases["measurement_id"], **{**bind, "graph_version_id": unconfirmed,
                                                              "graph_record_id": record2})
    assert error.value.code == "identity_not_confirmed"
    revoke(db, "reviewed_graph")  # the graph source revoked: the identity falls back, nothing is hidden by it
    assert by_value(listing(version)["measurements"])["66"]["identity"]["reason"] == "confirmed_identity_not_visible"


@real_checker
def test_the_change_feed_is_in_commit_order_and_withholds_revoked_sources(db, fake):
    chain(fake)
    first = measurement_reads.changes(True, after_seq=0, limit=2)
    assert first["more"] is True and len(first["items"]) == 2
    rest = measurement_reads.changes(True, after_seq=first["next_after"], limit=200)
    seqs = [i["seq"] for i in first["items"] + rest["items"]]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)) == 3
    assert {i["kind"] for i in first["items"] + rest["items"]} == {"created"}
    assert all(i["versions"]["checker"] == "biocheck.extraction_rules.v17" for i in rest["items"])
    assert measurement_reads.changes(True, after_seq=rest["next_after"])["items"] == []
    revoke(db)
    hidden = measurement_reads.changes(True, after_seq=0)
    assert hidden["items"] == [] and hidden["withheld"] == {"who_don": 3}


def test_a_graph_package_or_undeclared_source_is_never_read_for_measurements(db, fake, monkeypatch):
    who_version()
    with store.history_engine().begin() as conn:
        assert runs.measurable(conn, "who_don") is True
        assert runs.measurable(conn, "external_archive") is False  # not declared
    monkeypatch.setattr(registry, "MEASURE_SOURCES", ("who_don", "reviewed_graph"))
    with store.history_engine().begin() as conn:
        assert runs.measurable(conn, "reviewed_graph") is False  # a derived product never goes back in
    revoke(db)
    with store.history_engine().begin() as conn:
        assert runs.measurable(conn, "who_don") is False  # the permission first
    assert runs.plan(model=fake.model()) == 0


@real_checker
def test_a_wrong_checker_or_subject_is_never_adopted(db, fake):
    version = who_version()
    runs.plan(model=fake.model())
    read_all(fake)

    def wrong_subject(_request, response):
        response["subject"] = dict(response["subject"], version_id="f" * 64)

    check_all(RealBioCheck(answer=as_checker("biocheck.extraction_rules.v3")))
    check_all(RealBioCheck(answer=wrong_subject))
    assert rows(db, "SELECT count(*) AS n FROM derived_measurements")[0]["n"] == 0
    codes = {r["error_code"] for r in rows(db, "SELECT error_code FROM derived_extraction_checks")}
    assert "checker_mismatch" in codes
    assert listing(version)["measurements"] == []


def test_two_workers_never_take_the_same_window(db, fake):
    who_version()
    runs.plan(model=fake.model())
    routes = runs.routes_of(fake.model(), False)
    a, b = runs._claim(routes), runs._claim(routes)
    assert a and b and (a["run_id"], a["window_index"]) != (b["run_id"], b["window_index"])


def test_a_process_claims_only_the_windows_of_runs_by_its_own_producer(db, fake, monkeypatch):
    who_version()
    with monkeypatch.context() as earlier:  # the runs an earlier processor version planned, both routes
        earlier.setattr(mx, "PROCESSOR_VERSION", "datafabric.measure_extract.v1")
        assert runs.plan(model=fake.model(), with_rules=True) == 2
    other = fake.model("fake-measure-model-2")  # and the run of another model configuration
    assert runs.plan(model=other, producer_upgrade=True) == 1
    windows = "SELECT run_id, window_index, state, attempts FROM derived_measure_windows ORDER BY 1, 2"
    before = rows(db, windows)
    assert runs.process_once(model=fake.model(), with_rules=True)["state"] == "idle"
    assert fake.requests == [] and rows(db, windows) == before  # the old runs stay as they were: owed, unread
    assert rows(db, "SELECT count(*) AS n FROM derived_measure_window_attempts")[0]["n"] == 0
    assert runs.process_once(model=other)["state"] == "sent"  # the run of its own producer is read
    assert len(fake.requests) == 1 and fake.requests[0]["model"] == "fake-measure-model-2"


@real_checker
def test_the_routes_serve_the_receipts_to_a_reader(db, fake):
    from fastapi.testclient import TestClient

    from api.main import app

    version, items = chain(fake)
    client = TestClient(app)
    auth = {"Authorization": "Bearer test-reader-only"}
    cutoff = NOW().isoformat()
    got = client.get("/api/history/measurements", headers=auth, params={
        "source_id": "who_don", "is_demo": True, "mode": "current", "cutoff": cutoff, "version_ids": [version]})
    assert got.status_code == 200 and len(got.json()["measurements"]) == 3
    one = client.get(f"/api/history/measurements/{items[0]['measurement_id']}", headers=auth,
                     params={"is_demo": True, "mode": "current", "cutoff": cutoff})
    assert one.status_code == 200 and one.json()["measurement_id"] == items[0]["measurement_id"]
    feed = client.get("/api/history/measurements/changes", headers=auth, params={"is_demo": True})
    assert feed.status_code == 200 and len(feed.json()["items"]) == 3
    cov = client.get("/api/history/measure-coverage", headers=auth, params={
        "source_id": "who_don", "is_demo": True, "mode": "current", "cutoff": cutoff, "version_ids": [version]})
    assert cov.json()["versions"][version]["state"] == "complete"
    assert client.get("/api/history/measurements/changes", params={"is_demo": True}).status_code == 401
    bind = client.post(f"/api/history/measurements/{items[0]['measurement_id']}/identities", headers=auth, json={})
    assert bind.status_code == 403  # binding needs the administrator


# ---- late answers, re-granted permission, and the consumer contract ------------------------------------------


@real_checker
def test_a_late_verification_answer_is_audited_and_not_adopted(db, fake):
    version = who_version()
    runs.plan(model=fake.model())
    read_all(fake)

    def revoked_meanwhile(_request, _response):
        revoke(db)

    check_all(RealBioCheck(answer=revoked_meanwhile))
    late = rows(db, "SELECT status,error_code,items FROM derived_extraction_checks")
    assert late and late[0]["status"] == "unchecked" and late[0]["error_code"] == "source_unreadable"
    assert late[0]["items"] and late[0]["items"][0]["status"] == "consistent"  # the answer kept as audit
    assert rows(db, "SELECT count(*) AS n FROM derived_measurements")[0]["n"] == 0
    revoke(db, enabled=True)

    def lease_taken(_request, _response):
        with db.begin() as conn:
            conn.execute(text("UPDATE derived_extraction_check_tasks SET lease_token='another-worker'"))

    with db.begin() as conn:
        conn.execute(text("UPDATE derived_extraction_check_tasks SET next_check_at=clock_timestamp()"))
    check_all(RealBioCheck(answer=lease_taken))
    stale = rows(db, "SELECT error_code,items FROM derived_extraction_checks WHERE error_code='lease_lost'")
    assert len(stale) == 1 and stale[0]["items"]
    assert rows(db, "SELECT count(*) AS n FROM derived_measurements")[0]["n"] == 0
    with db.begin() as conn:  # the other worker's lease runs out: the task is checked once more, normally
        conn.execute(text("UPDATE derived_extraction_check_tasks SET lease_until=clock_timestamp()-interval '1 s',"
                          "next_check_at=clock_timestamp()"))
    check_all(RealBioCheck())
    assert len(listing(version)["measurements"]) == 3


@real_checker
def test_an_answer_for_a_superseded_run_is_history_at_once(db, fake, monkeypatch):
    version = who_version()
    runs.plan(model=fake.model())
    read_all(fake)  # the v12 run is read; its check waits
    with db.begin() as conn:
        conn.execute(text("UPDATE derived_extraction_check_tasks SET next_check_at=clock_timestamp()+interval '1 day'"))
    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "datafabric.clean_text.v13")
    clean_stage.process(time_budget_seconds=60)
    runs.plan(model=fake.model())
    read_all(fake)
    check_all(RealBioCheck())  # the v13 run is checked first
    newer = by_value(listing(version)["measurements"])
    assert {i["artifact"]["text_processor_version"] for i in newer.values()} == {"datafabric.clean_text.v13"}
    with db.begin() as conn:
        conn.execute(text("UPDATE derived_extraction_check_tasks SET next_check_at=clock_timestamp()"))
    check_all(RealBioCheck())  # the v12 answer arrives after the supersession
    old = rows(db, "SELECT measurement_id FROM derived_measurements WHERE text_processor_version=:t",
               t="datafabric.clean_text.v12")
    assert len(old) == 3  # recorded ...
    for row in old:
        got = measurement_reads.read_one(row["measurement_id"], True, "current", NOW())
        assert got["current"] is False and got["state_at_cutoff"] == "superseded"  # ... never current
    assert {k: v["measurement_id"] for k, v in by_value(listing(version)["measurements"]).items()} == {
        k: v["measurement_id"] for k, v in newer.items()}


@real_checker
def test_a_regranted_permission_is_checked_afresh_for_every_reader(db, fake):
    version, items = chain(fake)
    revoke(db)
    for read in (lambda: listing(version),
                 lambda: measurement_reads.read_one(items[0]["measurement_id"], True, "current", NOW()),
                 lambda: runs.coverage("who_don", True, [version], "current", NOW())):
        with pytest.raises(HistoryError) as error:
            read()
        assert error.value.status == 403
    assert measurement_reads.changes(True)["withheld"] == {"who_don": 3}
    revoke(db, enabled=True)
    # a new reader (another workspace) and the old one both read again with the permission of now
    assert len(listing(version)["measurements"]) == 3
    assert len(measurement_reads.changes(True, after_seq=0)["items"]) == 3
    revoke(db)
    with pytest.raises(HistoryError):  # nothing of the earlier answer is kept for the next reader
        listing(version)


class ReferenceConsumer:
    """The Fabric half of the linkage contract (tests/fixtures/measurement_feed_contract.json), executable: what
    Radar's series_sync must do with the feed and the pull so that both arrival orders end the same."""

    def __init__(self):
        self.cursor, self.inbox, self.topics, self.links = 0, {}, {}, []

    def sync(self):
        while True:
            page = measurement_reads.changes(True, after_seq=self.cursor, limit=2)
            for item in page["items"]:
                self.inbox.setdefault(item["measurement_id"], item["version_id"])
            self.cursor = page["next_after"]  # only after the page is stored
            self._link()
            if not page["more"]:
                return

    def triage(self, version, topic):
        self.topics[version] = topic
        for m in listing(version)["measurements"]:  # link_version: pull, in case the events are not there yet
            self.inbox.setdefault(m["measurement_id"], m["inputs"][0]["version_id"])
        self._link()

    def _link(self):
        for mid, version in self.inbox.items():
            link = (self.topics.get(version), mid)
            if link[0] is not None and link not in self.links:
                self.links.append(link)


@real_checker
def test_the_feed_lets_a_consumer_link_in_either_arrival_order(db, fake):
    contract = json.loads((Path(__file__).parent / "fixtures" / "measurement_feed_contract.json").read_text("utf-8"))
    expect = contract["linkage_expectation"]["who_marburg_fixture"]
    version = who_version()
    topic_first = ReferenceConsumer()
    topic_first.triage(version, "T1")  # the topic exists before any measurement
    _, items = chain(fake, version)
    measurement_first = ReferenceConsumer()
    measurement_first.sync()  # the measurements arrive before the topic
    assert measurement_first.links == []
    measurement_first.triage(version, "T1")
    topic_first.sync()
    assert sorted(topic_first.links) == sorted(measurement_first.links)
    assert len(topic_first.links) == expect["links"] == len(items) == expect["objects"]
    for consumer in (topic_first, measurement_first):  # replay and pull again: nothing more
        consumer.cursor = 0
        consumer.sync()
        consumer.triage(version, "T1")
        assert len(consumer.links) == expect["links"]
    feed = measurement_reads.changes(True)["items"]
    assert sum(i["kind"] == "created" for i in feed) == expect["created_events"]
    for item in feed:
        assert set(contract["feed"]["item_keys"]) <= set(item)
        assert set(item["versions"]) == set(contract["feed"]["version_keys"]) and item["kind"] in contract["feed"]["kinds"]
    for receipt in items:
        assert set(contract["pull"]["receipt_keys"]) <= set(receipt)


# ---- the checker-upgrade gap in replay, corrections without a target version -----------------------------------


@real_checker
def test_replay_never_shows_an_object_of_another_checker_as_ok(db, fake, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_MEASURE_CHECKER", "biocheck.extraction_rules.v15")
    version = who_version()
    runs.plan(model=fake.model())
    read_all(fake)
    check_all(RealBioCheck(answer=as_checker("biocheck.extraction_rules.v15")))
    old = by_value(listing(version)["measurements"])["66"]
    produced = NOW()
    assert len(listing(version, "operational_replay", produced)["measurements"]) == 3  # the configured checker
    monkeypatch.delenv("DATAFABRIC_MEASURE_CHECKER")  # v17 is configured now; when it took over is not recorded
    replay = listing(version, "operational_replay", produced)
    assert replay["measurements"] == [] and replay["excluded"] == {"checker_history_unknown": 3}
    one = measurement_reads.read_one(old["measurement_id"], True, "operational_replay", produced)
    assert one["current"] is False and one["not_current_reason"] == "checker_history_unknown"
    assert one["state_at_cutoff"] == "ok"  # its own events say nothing happened to it - but it is not current
    assert checks.backfill_measures() == 1
    check_all(RealBioCheck())
    later = by_value(listing(version, "operational_replay", NOW())["measurements"])
    assert later["66"]["check"]["checker_version"] == "biocheck.extraction_rules.v17"
    assert measurement_reads.read_one(old["measurement_id"], True, "operational_replay", NOW())[
        "not_current_reason"] == "rechecked_by_checker"

def unversioned_correction(key, *, relation="corrects", record="SYNTHETIC-unversioned", available="2026-11-02",
                           relations=True):
    raw = {"record_kind": "document", "title": "更正通报", "full_text": "前述通报中的病例数有误，以后续通报为准。",
           "material_kind": "official_notice"}
    if relations:
        raw["document_relations"] = [{"relation_type": relation, "target_source_id": "who_don",
                                      "target_source_record_id": WHO_ID}]
    records = [ArchiveRecord(source_record_id=record, record_kind="document", raw_content=json.dumps(raw, ensure_ascii=False))]
    proof = {"available_at": {"value": available, "precision": "day"}, "uri": "fixture://unversioned",
             "sha256": archive_manifest_hash(records), "reviewed": True, "review_note": "synthetic correction"}
    return store.import_archive(ArchiveImport(source_id="external_archive", is_demo=True, scope_description="corr",
                                              declared_count=1, records=records, proof=proof), key).version_ids[0]


@real_checker
def test_a_correction_without_a_target_version_leaves_the_record_unknown_until_it_is_resolved(db, fake):
    version, items = chain(fake)
    cases = by_value(items)["66"]
    before = NOW()
    corr = unversioned_correction("unversioned-1")
    scan()
    # no version known: every object of the corrected record is unknown, not current, out of the default list
    got = listing(version)
    assert got["measurements"] == [] and got["excluded"] == {"correction_target_version_unknown": 3}
    one = measurement_reads.read_one(cases["measurement_id"], True, "current", NOW())
    assert one["status"] == {"state": "unknown", "reason": "correction_target_version_unknown"}
    assert one["current"] is False and one["not_current_reason"] == "correction_target_version_unknown"
    assert one["unresolved_corrections"][0]["by"]["version_id"] == corr
    cov = runs.coverage("who_don", True, [version], "current", NOW())["versions"][version]
    assert [c["relation"] for c in cov["unresolved_corrections"]] == ["corrects"]
    # replay before the correction was seen: as it was
    assert measurement_reads.read_one(cases["measurement_id"], True, "operational_replay", before)["current"] is True
    # an explicit re-scan records nothing twice
    with store.history_engine().begin() as conn:
        measurements.rescan_corrections(conn, "external_archive")
    scan()
    (claim,) = rows(db, "SELECT id FROM derived_measure_correction_claims WHERE kind='asserted'")
    # resolved to a version: the normal events apply, and the record is no longer unknown
    measurements.resolve_correction(claim["id"], version, actor="tang.weiwen", basis="通报正文所指的版本")
    after = measurement_reads.read_one(cases["measurement_id"], True, "current", NOW())
    assert after["state_at_cutoff"] == "corrected" and after["current"] is True
    assert events_of(db, cases["measurement_id"]) == ["created", "corrected"]
    assert runs.coverage("who_don", True, [version], "current", NOW())["versions"][version][
        "unresolved_corrections"] == []
    with pytest.raises(HistoryError) as error:  # only a version of the record the correction names
        measurements.resolve_correction(claim["id"], "f" * 64, actor="tang.weiwen", basis="x")
    assert error.value.status in (404, 409)


@real_checker
def test_an_unresolved_correction_that_its_record_withdraws_no_longer_holds(db, fake):
    version, items = chain(fake)
    unversioned_correction("unversioned-a")
    scan()
    assert listing(version)["measurements"] == []
    unversioned_correction("unversioned-b", relations=False, available="2026-11-04")
    scan()
    assert len(listing(version)["measurements"]) == 3
    assert {r["kind"] for r in rows(db, "SELECT kind FROM derived_measure_correction_claims")} == {
        "asserted", "withdrawn"}


def two_relations(key, relations, record):
    raw = {"record_kind": "document", "title": "更正与撤回", "full_text": "前述通报的数字更正，并撤回其中一处。",
           "material_kind": "official_notice", "document_relations": relations}
    records = [ArchiveRecord(source_record_id=record, record_kind="document", raw_content=json.dumps(raw, ensure_ascii=False))]
    proof = {"available_at": {"value": "2026-11-02", "precision": "day"}, "uri": "fixture://both",
             "sha256": archive_manifest_hash(records), "reviewed": True, "review_note": "synthetic"}
    return store.import_archive(ArchiveImport(source_id="external_archive", is_demo=True, scope_description="both",
                                              declared_count=1, records=records, proof=proof), key).version_ids[0]


@real_checker
def test_a_correction_and_a_retraction_seen_at_the_same_moment_end_retracted_in_either_order(db, fake):
    version, items = chain(fake)
    cases = by_value(items)["66"]
    rel = {"target_source_id": "who_don", "target_source_record_id": WHO_ID, "target_version_id": version}
    # one correcting version states both: the same first-seen time for both events
    two_relations("both-1", [dict(rel, relation_type="corrects"), dict(rel, relation_type="retracts")], "SYNTHETIC-both")
    scan()
    got = measurement_reads.read_one(cases["measurement_id"], True, "current", NOW())
    assert got["state_at_cutoff"] == "retracted"
    assert sorted(events_of(db, cases["measurement_id"])[1:]) == ["corrected", "retracted"]
    seen = rows(db, "SELECT DISTINCT material_visible_at FROM derived_measurement_events WHERE kind IN "
                    "('corrected','retracted') AND measurement_id=:m", m=cases["measurement_id"])
    assert len(seen) == 1
    # stated the other way round in another version: the same state (a retraction is never undone by a correction)
    other = who_version(who_raw(uid=OTHER_ID), "who-b")
    _, later = chain(fake, other)
    rel_b = dict(rel, target_source_record_id=OTHER_ID, target_version_id=other)
    two_relations("both-2", [dict(rel_b, relation_type="retracts"), dict(rel_b, relation_type="corrects")],
                  "SYNTHETIC-both-b")
    scan()
    assert measurement_reads.read_one(by_value(later)["66"]["measurement_id"], True, "current", NOW())[
        "state_at_cutoff"] == "retracted"


@real_checker
def test_a_reread_never_merges_two_full_candidates_into_one_successor(db, fake):
    version, items = chain(fake)
    old = by_value(items)["66"]
    unitless = dict(CASES, dims=[{"kind": "disease", "text": "马尔堡病毒病"}])  # same quote, value, metric; other dims
    fake.measures = [unitless, CASES, DEATHS, RECOVERED]
    newer = fake.model("fake-measure-model-2")
    runs.plan(model=newer, producer_upgrade=True)
    read_all(fake, model=newer)
    check_all(RealBioCheck())
    (ev,) = rows(db, "SELECT superseded_by FROM derived_measurement_events WHERE measurement_id=:m "
                     "AND kind='superseded_by_producer'", m=old["measurement_id"])
    successor = measurement_reads.read_one(ev["superseded_by"].strip(), True, "current", NOW())
    assert successor["dims"] == old["dims"] and successor["unit"] == old["unit"]  # the same full candidate
    successors = rows(db, "SELECT superseded_by, count(*) AS n FROM derived_measurement_events "
                          "WHERE kind='superseded_by_producer' GROUP BY superseded_by")
    assert all(r["n"] == 1 for r in successors)  # one to one


# ---- the two routes and an explicit scope (task book 01, 2.7) ----------------------------------------------------

EN_SENTENCE = "As of 28 October 2026, Rwanda has reported 66 cumulative confirmed cases of Marburg virus disease."
EN_MODEL = {"object_text": "Marburg virus disease", "metric_text": "cumulative confirmed cases of Marburg virus disease",
            "value_text": "66", "unit_text": "cases", "period_text": "As of 28 October 2026", "period": "2026-10-28",
            "period_type": "point", "scope_text": "Rwanda", "kind": "cumulative", "family": "case_count",
            "quote": EN_SENTENCE, "dims": [{"kind": "disease", "text": "Marburg virus disease"}]}


def en_raw(uid):
    return {"Id": uid, "Title": "Marburg virus disease - Rwanda", "Summary": f"<p>{EN_SENTENCE}</p>"}


def mark_background(pg, version):
    """What the receipt of a bounded backfill does: list the version as one its run created."""
    receipt = {"backfill": {"background": True, "background_version_ids": [version]}}
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_runs SET coverage = coverage || CAST(:c AS jsonb) "
                          "WHERE version_ids @> CAST(:v AS jsonb)"), {"c": json.dumps(receipt), "v": json.dumps([version])})


def read_routes(fake, scope=None):
    for _ in range(100):
        if runs.process_once(model=fake.model(), scope=scope, with_rules=True)["state"] == "idle":
            return
    raise AssertionError("windows never ran out")


def test_a_backfill_version_is_measured_only_when_a_list_names_it_and_the_routes_share_one_entry(db):
    fake = FakeModel([EN_MODEL])
    try:
        live = who_version(en_raw(WHO_ID), "who-live")
        background = who_version(en_raw(OTHER_ID), "who-background")
        mark_background(db, background)
        assert runs.plan(model=fake.model(), with_rules=True) == 2  # the live version: one run per route
        assert {r["version_id"].strip() for r in rows(db, "SELECT version_id FROM derived_measure_runs")} == {live}
        scope = runs.Scope.parse({"versions": [background]})
        receipt = runs.plan_scope(scope, model=fake.model(), with_rules=True)
        assert {name: list(route["planned"]) for name, route in receipt["routes"].items()} == {
            "model": [background], "rules": [background]}
        selected = rows(db, "SELECT producer_identity, selection FROM derived_measure_runs WHERE version_id=:v",
                        v=background)
        assert [mx.route_of(r["producer_identity"]) for r in selected].count(mx.RULES) == 1
        assert all(r["selection"] == scope.selection(background) for r in selected)
        again = runs.plan_scope(scope, model=fake.model(), with_rules=True)
        assert all(r["planned"] == {} and r["already_planned"] == [background] for r in again["routes"].values())
        read_routes(fake, scope)  # through the list: the listed version only, the live version waits
        assert len(fake.requests) == 1 and rows(db, "SELECT count(*) AS n FROM derived_measure_window_attempts "
                                                    "WHERE run_id IN (SELECT run_id FROM derived_measure_runs "
                                                    "WHERE version_id=:v)", v=live)[0]["n"] == 0
        read_routes(fake)
        assert len(fake.requests) == 2  # the rules route sends no request
        readings = rows(db, "SELECT version_id, producer_identity FROM derived_measure_readings")
        assert sorted((r["version_id"].strip(), mx.route_of(r["producer_identity"])) for r in readings) == sorted(
            [(background, "model"), (background, "rules"), (live, "model"), (live, "rules")])
        covered = runs.coverage("who_don", True, [background], "current", NOW())["versions"][background]
        assert set(covered["routes"]) == {"model", "rules"} and covered["routes"]["rules"]["selection"] is not None
        agreed = covered["route_agreement"]  # one reading made twice: here the metric differs, so both stay
        assert agreed["independent_evidence"] is False and [p["verdict"] for p in agreed["pairs"]] == ["conflict"]
    finally:
        fake.close()


def test_a_list_that_names_windows_leaves_the_others_out_of_scope_and_reports_what_it_could_not_plan(db, fake):
    from flows import measure_extract as worker

    version, ghost = who_version(), "f" * 64
    assert runs.plan_scope(runs.Scope.parse({"versions": [{"version_id": version, "windows": [999]}, ghost]}),
                           model=fake.model())["routes"]["model"]["not_planned"] == {
        version: "window_out_of_range", ghost: "version_unknown"}
    assert rows(db, "SELECT count(*) AS n FROM derived_measure_runs")[0]["n"] == 0
    scope = runs.Scope.parse({"versions": [{"version_id": version, "windows": [1]}]})
    done = worker.run_scope(scope, model=fake.model(), with_rules=False)
    states = {r["window_index"]: r["state"] for r in rows(db, "SELECT window_index, state FROM derived_measure_windows")}
    assert states.pop(1) == "sent" and set(states.values()) == {"skipped"}
    assert len(fake.requests) == done["requests"] == 1
    covered = runs.coverage("who_don", True, [version], "current", NOW())["versions"][version]
    assert covered["state"] == "complete" and covered["read"] == 1 and covered["owed"] == []
    assert covered["out_of_scope"] == covered["windows_total"] - 1 and covered["selection"]["windows"] == [1]


def test_a_list_claims_only_the_windows_it_names_of_a_run_the_standing_queue_already_planned(db, fake):
    version = who_version()
    assert runs.plan(model=fake.model()) == 1  # the standing queue plans every window of the version
    scope = runs.Scope.parse({"versions": [{"version_id": version, "windows": [0]}]})
    receipt = runs.plan_scope(scope, model=fake.model())["routes"]["model"]
    assert receipt["planned"] == {} and receipt["already_planned"] == [version]
    read_routes(fake, scope)
    states = {r["window_index"]: r["state"] for r in rows(db, "SELECT window_index, state FROM derived_measure_windows")}
    assert len(fake.requests) == 1 and {i for i, s in states.items() if s == "pending"} == {1}
    read_routes(fake)  # the standing queue still reads the rest
    assert len(fake.requests) == len(states)


def test_a_list_plans_a_run_over_an_older_producers_only_when_asked_to_upgrade(db, fake, monkeypatch):
    listed = who_version()
    who_version(who_raw(uid=OTHER_ID), "who-other")  # a second version, not on the list
    with monkeypatch.context() as earlier:  # both versions were planned by an earlier processor version
        earlier.setattr(mx, "PROCESSOR_VERSION", "datafabric.measure_extract.v1")
        assert runs.plan(model=fake.model()) == 2
    sql = "SELECT * FROM derived_measure_runs ORDER BY run_id"
    before = rows(db, sql)
    old = next(r["run_id"] for r in before if r["version_id"].strip() == listed)
    scope = runs.Scope.parse({"versions": [{"version_id": listed, "windows": [0]}]})
    held = runs.plan_scope(scope, model=fake.model())["routes"]["model"]
    assert held["planned"] == {} and held["already_planned"] == [listed] and rows(db, sql) == before
    upgraded = runs.plan_scope(scope, model=fake.model(), producer_upgrade=True)["routes"]["model"]
    assert list(upgraded["planned"]) == [listed] and upgraded["already_planned"] == []
    after = rows(db, sql)
    assert [r for r in after if r in before] == before  # the old runs keep their identity, state and windows
    (new,) = [r for r in after if r not in before]  # one new run only: the version outside the list got none
    assert new["version_id"].strip() == listed and new["supersedes_run_id"] == old
    assert new["producer_identity"] == runs.producer_key(fake.model())["identity"]
    assert new["selection"] == scope.selection(listed)
    again = runs.plan_scope(scope, model=fake.model(), producer_upgrade=True)["routes"]["model"]
    assert again["planned"] == {} and again["already_planned"] == [listed]


@real_checker
def test_the_rules_route_alone_gives_a_checked_object_that_names_its_route(db):
    version = who_version(en_raw(WHO_ID), "who-rules")
    assert runs.plan(with_rules=True) == 1 and rows(db, "SELECT model_config_sha256 FROM derived_measure_runs") == [
        {"model_config_sha256": None}]  # no model is configured: only the rules route has a run
    for _ in range(100):
        if runs.process_once(with_rules=True)["state"] == "idle":
            break
    check_all(RealBioCheck())
    (item,) = listing(version)["measurements"]
    assert item["value_text"] == "66" and item["metric"] == "cumulative confirmed cases" and item["scope"] == "Rwanda"
    assert item["check"]["status"] == "consistent" and item["state_at_cutoff"] == "ok" and item["current"] is True
    assert item["versions"]["producer"] == mx.rules_identity("who.explicit-count.v1")
