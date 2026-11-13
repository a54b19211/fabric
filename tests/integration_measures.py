"""WHO DON measurements on real PostgreSQL: a fixed clean text -> windows -> readings -> objects (unchecked) -> the
two read routes in the shapes the radar checks (series_sync._valid_page / _valid_receipt, copied below, and the shared
contract fixture tests/fixtures/measurement_feed_contract.json).

The model is a loopback OpenAI-compatible server in this process (no model, no network beyond 127.0.0.1)."""

import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import llm_transport as lt
import pytest
from fastapi.testclient import TestClient
from integration_history import pg, pg_engine  # noqa: F401  (fixtures)
from sqlalchemy import text

from clean import clean_stage, measurement_reads
from clean import measure_extract as mx
from clean import measure_runs as runs
from run import measure
from store import history as store
from store.history_models import ArchiveImport, ArchiveRecord, archive_manifest_hash

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "measurement_feed_contract.json").read_text("utf-8"))
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731  (as the other suites)
FILLER = "".join(f"<p>第{n}次更新：应对小组继续开展接触者追踪、感染预防和社区参与工作，实验室检测能力已得到加强。</p>"
                 for n in "一二三四五六七八九十" * 4)
SENTENCE = "截至2026年10月28日，卢旺达卫生部在卢旺达全国累计共报告马尔堡病毒病{what}{n}例"
TABLES = ("derived_measurement_events,derived_measurement_feed,derived_measurements,derived_measure_readings,"
          "derived_measure_window_attempts,derived_measure_windows,derived_measure_runs")


def who_raw(uid="8c6e4a3e-2b0e-4b8e-9d55-1d1b1c7f5a01"):
    cases, deaths, recovered = (SENTENCE.format(what=w, n=n) for w, n in (("确诊病例", 66), ("死亡病例", 15),
                                                                          ("治愈病例", 40)))
    overview = f"<h2>按报告日统计</h2><p>{cases}。</p>" + FILLER + f"<p>{deaths}。</p><p>{recovered}。</p>"
    return {"Id": uid, "Title": "马尔堡病毒病 - 卢旺达", "Overview": overview}


def measure_of(what, n, **extra):
    return {"object_text": "卢旺达卫生部", "metric_text": f"报告马尔堡病毒病{what}", "value_text": str(n), "unit_text": "例",
            "period_text": "截至2026年10月28日", "period": "2026-10-28", "period_type": "point",
            "scope_text": "在卢旺达全国", "kind": "cumulative", "family": "case_count",
            "quote": SENTENCE.format(what=what, n=n), "dims": [{"kind": "disease", "text": "马尔堡病毒病"}], **extra}


# cases with a dimension from the window's heading; deaths without the unit the text writes (the D1 case)
MEASURES = [measure_of("确诊病例", 66, dims=[{"kind": "disease", "text": "马尔堡病毒病"},
                                          {"kind": "date_basis", "text": "按报告日统计"}]),
            measure_of("死亡病例", 15, unit_text=""), measure_of("治愈病例", 40, dims=[])]


class FakeModel:
    """POST /v1/chat/completions on 127.0.0.1: each window is answered with the measures whose quote is in its text;
    while `status` is set, every request is answered with that HTTP status instead."""

    def __init__(self):
        self.requests, self.status = [], None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                window = json.loads(body["messages"][1]["content"])["windows"][0]["text"]
                content = json.dumps({"items": [{"index": 1, "measures": [m for m in MEASURES if m["quote"] in window]}]})
                raw = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}).encode()
                self.send_response(fake.status or 200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def model(self):
        provider = lt.Provider(kind="openai_compatible", base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
                               model="fake-measure-model", api_key="synthetic-key", timeout_seconds=2.0,
                               endpoint_id="loopback-fake")
        return mx.Model(provider, {"kind": "openai_compatible", "model": "fake-measure-model",
                                   "endpoint_id": "loopback-fake", "thinking": False, "temperature": 0.0,
                                   "max_tokens": 2000})


@pytest.fixture
def db(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["who_don","external_archive"]')
    with pg.begin() as conn:
        conn.execute(text("TRUNCATE " + TABLES + " CASCADE"))
    return pg


@pytest.fixture
def fake():
    model = FakeModel()
    yield model
    model.server.shutdown()
    model.server.server_close()


def rows(pg, sql, **params):
    with pg.begin() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def who_version(raw=None, key="who-a"):
    """A fixed WHO DON version (a demo archive) and its clean text."""
    raw = raw or who_raw()
    records = [ArchiveRecord(source_record_id=raw["Id"], record_kind="document",
                             raw_content=json.dumps(raw, ensure_ascii=False))]
    proof = {"available_at": {"value": "2026-10-29", "precision": "day"}, "uri": "fixture://who-don-archive",
             "sha256": archive_manifest_hash(records), "reviewed": True, "review_note": "synthetic WHO DON fixture"}
    run = store.import_archive(ArchiveImport(source_id="who_don", is_demo=True, scope_description="synthetic WHO DON",
                                             declared_count=1, records=records, proof=proof), key)
    clean_stage.process(time_budget_seconds=60)
    return run.version_ids[0]


def listing(version, **kw):
    return measurement_reads.for_versions("who_don", True, [version], "current", NOW(), **kw)["measurements"]


def by_value(items):
    return {i["value_text"]: i for i in items}


def valid_page(page):  # BioRadar services/bioradar/research/series_sync.py _valid_page
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list) or not isinstance(page.get("next_after"), int):
        return False
    return all(re.fullmatch(r"[0-9a-f]{64}", i.get("measurement_id") or "") and isinstance(i.get("version_id"), str)
               and isinstance(i.get("source_id"), str) and i.get("kind") in CONTRACT["feed"]["kinds"]
               and isinstance(i.get("seq"), int) for i in items)


def valid_receipt(r):  # BioRadar series_sync._valid_receipt
    return (re.fullmatch(r"[0-9a-f]{64}", r.get("measurement_id") or "") is not None
            and isinstance(r.get("current"), bool) and isinstance(r.get("inputs"), list) and bool(r["inputs"])
            and all(isinstance(r.get(k), dict) for k in ("identity", "check", "period", "versions")))


def test_who_case_counts_become_unchecked_objects_and_a_rerun_changes_nothing(db, fake):
    version = who_version()
    counts = measure.run_round(model=fake.model(), with_rules=False)
    assert counts["planned"] == 1 and counts["windows"]["sent"] > 1 and counts["windows"]["idle"] == 1
    got = by_value(listing(version))
    assert set(got) == {"66", "15", "40"}
    cases = got["66"]
    assert (cases["object"], cases["metric"], cases["unit"], cases["scope"]) == (
        "卢旺达卫生部", "报告马尔堡病毒病确诊病例", "例", "在卢旺达全国")
    assert cases["period"] == {"type": "point", "precision": "day", "value": "2026-10-28", "start": None, "end": None}
    assert {d["kind"]: d["check"] for d in cases["dims"]} == {"disease": "unchecked", "date_basis": "unchecked"}
    assert cases["check"]["status"] == "unchecked" and cases["current"] is True and cases["state_at_cutoff"] == "ok"
    assert cases["inputs"][0]["version_id"] == version and cases["versions"]["producer"] == mx.model_identity(fake.model())
    assert got["15"]["unit"] == "unknown"  # the model left the unit out: the object says so and flows on
    (clean,) = rows(db, "SELECT text FROM derived_clean_text WHERE version_id=:v", v=version)
    (reading,) = rows(db, "SELECT quote_start, quote_end FROM derived_measure_readings WHERE reading_id=:m",
                      m=cases["measurement_id"])
    assert clean["text"][reading["quote_start"]:reading["quote_end"]] == cases["quote"]
    assert rows(db, "SELECT state FROM derived_measure_runs") == [{"state": "complete"}]
    sent, before = len(fake.requests), rows(db, "SELECT count(*) AS n FROM derived_measurement_events")
    assert measure.run_round(model=fake.model(), with_rules=False)["planned"] == 0
    assert len(fake.requests) == sent and rows(db, "SELECT count(*) AS n FROM derived_measurement_events") == before


def test_a_new_clean_text_supersedes_the_objects_of_the_earlier_run(db, fake):
    version = who_version()
    measure.run_round(model=fake.model(), with_rules=False)
    old = by_value(listing(version))
    with db.begin() as conn:  # what a later cleaner might give: one more paragraph first
        conn.execute(text("""
            INSERT INTO derived_clean_text(version_id,processor_version,attempt,source_id,is_demo,source_record_id,
              status,text,chars,text_sha256,segments)
            SELECT version_id,processor_version,attempt+1,source_id,is_demo,source_record_id,status,
                   '本期为更新后的疫情通报。' || chr(10) || text,chars+13,
                   encode(sha256(convert_to('本期为更新后的疫情通报。' || chr(10) || text,'UTF8')),'hex'),'[]'
              FROM derived_clean_text WHERE version_id=:v"""), {"v": version})
    assert measure.run_round(model=fake.model(), with_rules=False)["planned"] == 1
    new = by_value(listing(version))
    assert set(new) == {"66", "15", "40"} and not set(i["measurement_id"] for i in new.values()) & {
        i["measurement_id"] for i in old.values()}
    history = {i["measurement_id"]: i for i in listing(version, include_history=True)}
    for value, item in old.items():
        gone = history[item["measurement_id"]]
        assert gone["current"] is False and gone["not_current_reason"] == "superseded_by_processor"
        assert gone["status"]["superseded_by"] == new[value]["measurement_id"]
    kinds = [i["kind"] for i in measurement_reads.changes(True)["items"]]
    assert kinds == ["created"] * 3 + ["created"] * 3 + ["superseded_by_processor"] * 3


def test_the_rules_route_reads_an_english_notice(db):
    quote = ("As of 28 October 2026, a cumulative total of 66 confirmed cases of Marburg virus disease have been "
             "reported in Rwanda.")
    version = who_version({"Id": "8c6e4a3e-2b0e-4b8e-9d55-1d1b1c7f5a02", "Title": "Marburg virus disease - Rwanda",
                           "Summary": f"<p>{quote}</p>"}, key="who-en")
    measure.run_round(model=None, with_rules=True)
    (item,) = listing(version)
    assert (item["metric"], item["value_text"], item["unit"], item["measure_kind"]) == (
        "confirmed cases", "66", "cases", "cumulative")
    assert item["versions"]["producer"] == mx.RULES_IDENTITY and item["check"]["status"] == "unchecked"


def test_a_failed_window_is_retried_and_then_owed(db, fake):
    who_version()
    fake.status = 500
    runs.plan(model=fake.model(), with_rules=False)
    for attempt in range(1, runs.MAX_ATTEMPTS + 1):
        with db.begin() as conn:
            conn.execute(text("UPDATE derived_measure_windows SET next_at=clock_timestamp()+CASE WHEN window_index=0 "
                              "THEN interval '0 s' ELSE interval '1 day' END"))  # only window 0 is due
        assert runs.process_once(model=fake.model(), with_rules=False)["state"] == (
            "failed" if attempt < runs.MAX_ATTEMPTS else "failed_final")
    fake.status = None
    with db.begin() as conn:
        conn.execute(text("UPDATE derived_measure_windows SET next_at=clock_timestamp()"))
    measure.run_round(model=fake.model(), with_rules=False)
    assert rows(db, "SELECT state, reason FROM derived_measure_runs") == [{"state": "partial", "reason": "windows_owed"}]


def test_a_fault_of_a_route_fails_its_window_not_the_worker(db, monkeypatch):
    who_version()
    monkeypatch.setattr(mx, "rules_measures", lambda *args: 1 / 0)
    counts = measure.run_round(model=None, with_rules=True)
    assert counts["windows"]["failed"] >= 1 and counts["windows"]["idle"] == 1
    errors = {r["error"] for r in rows(db, "SELECT error FROM derived_measure_window_attempts")}
    assert errors == {"route_error:ZeroDivisionError"}


def test_a_window_another_worker_holds_is_skipped(db, fake):
    who_version()
    runs.plan(model=fake.model(), with_rules=False)
    with db.connect() as other:
        held = other.execute(text("SELECT window_index FROM derived_measure_windows WHERE window_index=0 FOR UPDATE"))
        assert held.scalar() == 0
        runs.process_once(model=fake.model(), with_rules=False)
        read = rows(db, "SELECT window_index FROM derived_measure_windows WHERE state='sent'")
        assert read == [{"window_index": 1}]
        other.rollback()


def test_the_routes_serve_what_the_radar_accepts(db, fake, monkeypatch):
    from api.main import app

    version = who_version()
    measure.run_round(model=fake.model(), with_rules=False)
    client, auth = TestClient(app), {"Authorization": "Bearer test-reader-only"}
    page = client.get("/api/history/measurements/changes", params={"is_demo": "true", "limit": 2}, headers=auth).json()
    assert valid_page(page) and page["more"] is True and page["next_after"] == 2 and page["withheld"] == {}
    feed = page["items"] + client.get("/api/history/measurements/changes", params={
        "is_demo": "true", "after_seq": 2}, headers=auth).json()["items"]
    expect = CONTRACT["linkage_expectation"]["who_marburg_fixture"]
    assert sum(i["kind"] == "created" for i in feed) == expect["created_events"]
    for item in feed:
        assert set(CONTRACT["feed"]["item_keys"]) <= set(item)
        assert set(item["versions"]) == set(CONTRACT["feed"]["version_keys"])
    pulled = client.get("/api/history/measurements", params={
        "source_id": "who_don", "is_demo": "true", "mode": "current", "cutoff": NOW().isoformat(),
        "version_ids": [version], "include_history": "true"}, headers=auth).json()["measurements"]
    assert len(pulled) == expect["objects"] and all(map(valid_receipt, pulled))
    assert all(set(CONTRACT["pull"]["receipt_keys"]) <= set(r) for r in pulled)
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["external_archive"]')  # who_don revoked
    withheld = client.get("/api/history/measurements/changes", params={"is_demo": "true"}, headers=auth).json()
    assert withheld["items"] == [] and withheld["withheld"] == {"who_don": 3} and withheld["next_after"] == 3
    assert client.get("/api/history/measurements", params={
        "source_id": "who_don", "is_demo": "true", "mode": "current", "cutoff": NOW().isoformat(),
        "version_ids": [version]}, headers=auth).status_code == 403
