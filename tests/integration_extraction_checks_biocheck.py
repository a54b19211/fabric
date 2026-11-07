"""v3 6.2 (T-29 P2) on real PostgreSQL against the real BioCheck v3 rules: the review regressions (r116/r118,
r123/r127, r128) kept runnable from this repository. BioCheck is run in-process through its own HTTP app
(FastAPI TestClient, no network, no LLM) in a separate Python, one request per call, located by:

    DATAFABRIC_TEST_BIOCHECK_SRC=/path/to/BioCheck checkout (app/, tests/conftest.py)
    DATAFABRIC_TEST_BIOCHECK_PYTHON=/path/to/a Python with BioCheck's dependencies (default: this one)

Without DATAFABRIC_TEST_BIOCHECK_SRC every test here is skipped - reported as skipped, never as a pass."""

import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import httpx
import pytest
from integration_extraction_checks import document, drain, env, pg, pg_engine, stored  # noqa: F401  (fixtures)
from integration_index_publication import add_document, profile, rows
from sqlalchemy import text

from db import extraction_checks as checks

SRC = os.environ.get("DATAFABRIC_TEST_BIOCHECK_SRC")
PYTHON = os.environ.get("DATAFABRIC_TEST_BIOCHECK_PYTHON") or sys.executable
pytestmark = pytest.mark.skipif(not SRC, reason="DATAFABRIC_TEST_BIOCHECK_SRC (a BioCheck checkout) is not set")

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


def biocheck(body):
    environ = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.path.join(SRC, "tests"))
    p = subprocess.run([PYTHON, "-c", DRIVER], input=json.dumps(body), capture_output=True, text=True, env=environ,
                       cwd=SRC, timeout=30)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout)


class RealBioCheck:
    """The real checker behind Fabric's client. before(body) may alter what reaches BioCheck (a simulated producer
    fault); after(request, response, count) may alter the answer (a simulated faulty checker)."""

    def __init__(self, after=None, before=None):
        self.requests, self.responses, self.after, self.before = [], [], after, before

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        sent = copy.deepcopy(body)
        if self.before:
            self.before(sent)
        result = biocheck(sent)
        assert result["status"] == 200, result
        if self.after:
            self.after(body, result["body"], len(self.requests))
        self.responses.append(copy.deepcopy(result["body"]))
        return httpx.Response(result["status"], json=result["body"])

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def view(version):
    return checks.for_versions("external_archive", True, [version], "current", datetime.now(timezone.utc))["versions"][version]


def many_document(pg):
    version = add_document("NCT87654321", summary="Registered trials " + " ".join(
        "NCT%08d" % n for n in range(10000001, 10000066)))
    profile()
    n = rows(pg, "SELECT count(*) AS n FROM derived_identifiers WHERE version_id=:v", {"v": version})[0]["n"]
    assert n > 50, n
    return version, n


# r116/r118: the real round trip, every batch, checker binding, permission and lease

def test_real_round_trip_keeps_the_rule_outcomes(pg, env):
    version = document()
    real = RealBioCheck()
    assert set(drain(real)) == {"done"}
    assert real.requests and all(r["checker_version"] == "biocheck.extraction_rules.v3" for r in real.responses)
    assert any(i["kind"] == "identifier" and i["status"] == "consistent" for r in real.responses for i in r["items"])
    assert all(i["status"] == "unchecked" for r in real.responses for i in r["items"] if i["kind"] == "annotation")
    assert view(version)["identifiers"]["items"]


def test_readback_holds_every_batch_not_only_the_last(pg, env):
    version, n = many_document(pg)
    real = RealBioCheck()
    drain(real)
    assert sum(1 for r in real.requests if r["items"][0]["kind"] == "identifier") >= 2
    got = view(version)["identifiers"]
    assert len(got["items"]) == n, (n, len(got["items"]), got["counts"])


def test_an_answer_of_another_checker_is_never_completed(pg, env, monkeypatch):
    version = document()
    monkeypatch.setenv("DATAFABRIC_EXTRACTION_CHECKER", "biocheck.extraction_rules.v4")
    drain(RealBioCheck())
    assert not [r for r in stored(pg, version) if r["status"] == "completed"], stored(pg, version)


def test_a_checker_upgrade_invalidates_old_results_and_queues_work(pg, env, monkeypatch):
    version = document()
    drain(RealBioCheck())
    monkeypatch.setenv("DATAFABRIC_EXTRACTION_CHECKER", "biocheck.extraction_rules.v4")
    checks.backfill()
    got = view(version)["identifiers"]
    pending = rows(pg, "SELECT count(*) AS n FROM derived_extraction_check_tasks WHERE state IN ('queued','waiting')")[0]["n"]
    assert got["state"] == "unchecked" and pending > 0, (got["state"], got["checker_version"], pending)


def test_permission_is_rechecked_between_batches_and_before_adoption(pg, env):
    version, _ = many_document(pg)

    def revoke(request, response, count):
        if count == 1:
            with pg.begin() as conn:
                conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"))

    real = RealBioCheck(revoke)
    drain(real)
    assert len(real.requests) == 1, "more source content was sent after permission was withdrawn"
    assert not [r for r in stored(pg, version) if r["status"] == "completed"]


@pytest.mark.parametrize("fault", ["subject", "field_status", "malformed_item"])
def test_an_answer_with_wrong_binding_or_fields_is_unchecked(pg, env, fault):
    version = document()

    def corrupt(request, response, count):
        if fault == "subject":
            response["subject"]["version_id"] = "f" * 64
        elif fault == "field_status":
            for item in response["items"]:
                item["status"] = "consistent"
                item["fields"] = {"identifier": {"result": "mismatch", "reason": "synthetic contradiction"}}
        else:
            response["items"] = [None]

    drain(RealBioCheck(corrupt))
    assert not [r for r in stored(pg, version) if r["status"] == "completed"], stored(pg, version)
    assert "invalid_response" in {r["error_code"] for r in stored(pg, version)}


def test_a_worker_that_lost_its_lease_cannot_publish_late(pg, env):
    version = document()

    def lose(request, response, count):
        with pg.begin() as conn:
            conn.execute(text("UPDATE derived_extraction_check_tasks SET lease_token='replacement-owner',"
                              "lease_until=clock_timestamp()+interval '5 min' WHERE lease_token IS NOT NULL"))

    checks.process_once(client=RealBioCheck(lose).client())
    assert not [r for r in stored(pg, version) if r["status"] == "completed"], stored(pg, version)


# r123/r127: content actually checked; nothing cut to fit

def test_a_locator_match_alone_is_not_a_checked_identifier(pg, env):
    version = document()

    def strip(request, response, count):
        for item in response["items"]:
            item["fields"] = {"locator": item["fields"]["locator"]}
            item["status"] = "consistent"

    drain(RealBioCheck(strip))
    assert not [r for r in stored(pg, version) if r["status"] == "completed"]


def test_an_appended_301_character_value_is_not_replaced_by_its_valid_prefix(pg, env):
    literal = "10.1234/" + "a" * 292
    version = add_document("NCT01234567", summary="Identifier " + literal)
    profile()
    doi = rows(pg, "SELECT value FROM derived_identifiers WHERE version_id=:v AND kind='doi'", {"v": version})
    assert [r["value"] for r in doi] == [literal], doi
    with pg.begin() as conn:  # a faulty producer row, appended: the table stays append-only
        conn.execute(text(
            "INSERT INTO derived_identifiers (version_id,processor_version,text_processor_version,kind,value,"
            "start_offset,end_offset) SELECT version_id,processor_version,text_processor_version,kind,value||'z',"
            "start_offset,end_offset FROM derived_identifiers WHERE version_id=:v AND kind='doi'"), {"v": version})
    produced = rows(pg, "SELECT kind,value,start_offset,end_offset FROM derived_identifiers WHERE version_id=:v "
                        "ORDER BY start_offset,kind,value", {"v": version})
    bad = [(n, r) for n, r in enumerate(produced, 1) if r["kind"] == "doi" and r["value"] == literal + "z"]
    assert len(bad) == 1 and bad[0][1]["end_offset"] - bad[0][1]["start_offset"] == 300, bad
    bad_id = "i" + str(bad[0][0])
    real = RealBioCheck()
    drain(real)
    sent = [i["fields"]["identifier"]["value"] for r in real.requests for i in r["items"] if i["id"] == bad_id]
    assert sent == [], "the 301-character value was sent (cut or whole) although it exceeds the contract"
    assert [i["fields"]["identifier"]["value"] for r in real.requests for i in r["items"]
            if i["kind"] == "identifier" and i["fields"]["scheme"] == "doi"] == [literal]
    got = view(version)["identifiers"]
    mine = [i for i in got["items"] if i["id"] == bad_id]
    assert [(i["status"], i["reason"]) for i in mine] == [("unchecked", "value_exceeds_contract")], mine


# r128: v3's explicit no-location negative is an extraction result, not a protocol fault

def test_a_real_no_location_negative_is_adopted_as_inconsistent(pg, env):
    version = document()

    def misquote(body):  # the producer's quote no longer matches its locator
        for item in body["items"]:
            if item["kind"] == "identifier":
                item["quote"] = "other"

    real = RealBioCheck(before=misquote)
    assert set(drain(real)) == {"done"}
    answered = [i for r in real.responses for i in r["items"] if i["kind"] == "identifier"]
    assert answered and all(i["status"] == "inconsistent" and i["fields"]["locator"]["result"] == "mismatch"
                            for i in answered), answered
    assert not {r["error_code"] for r in stored(pg, version)} - {None}, stored(pg, version)
    got = view(version)["identifiers"]
    assert got["state"] == "checked" and got["counts"].get("inconsistent") == len(answered), got["counts"]


SUBJECT = {"source_id": "external_archive", "record_id": "synthetic", "version_id": "a" * 64, "raw_sha256": "b" * 64,
           "is_demo": True, "artifact": {"processor_version": "test.v1", "text_sha256": "c" * 64}}
TEXT = "Registered as NCT01234567; 32 of 120 patients responded."
UNKNOWN = {"state": "unknown"}


def _span(piece):
    at = TEXT.index(piece)
    return {"passage": "p1", "start": at, "end": at + len(piece)}


def _item(kind):
    base = {"id": kind[0] + "1", "kind": kind, "producer": {"name": "test", "version": "1"}}
    if kind == "identifier":
        return {**base, "locator": _span("NCT01234567"), "quote": "NCT01234567",
                "fields": {"scheme": "nct", "identifier": {"state": "stated", "value": "NCT01234567",
                                                           "span": _span("NCT01234567")}}}
    if kind == "annotation":
        return {**base, "locator": _span("patients"), "quote": "patients", "fields": {"label": "human"}}
    if kind == "claim":
        return {**base, "locator": _span("32 of 120 patients responded"), "quote": "32 of 120 patients responded",
                "fields": {"statement": {"state": "stated", "value": "32 of 120 patients responded",
                                         "span": _span("32 of 120 patients responded")}, "polarity": "affirmed"}}
    return {**base, "locator": _span("32 of 120 patients responded"), "quote": "32 of 120 patients responded",
            "fields": {"object": UNKNOWN, "metric": UNKNOWN, "unit": UNKNOWN, "period": UNKNOWN, "scope": UNKNOWN,
                       "denominator": UNKNOWN, "value": {"state": "stated", "value": "32", "span": _span("32")},
                       "measure_kind": "cumulative"}}


@pytest.mark.parametrize("kind", ["identifier", "annotation", "claim", "measurement"])
def test_the_verdict_follows_the_real_v3_answer_of_each_kind(kind, monkeypatch):
    """For every kind: the located answer of v3 is adoptable; its no-location negative (quote not at the locator)
    is adoptable and inconsistent; the negative with a located-looking status, or a located answer missing one of
    the kind's fields, is not."""
    monkeypatch.delenv("DATAFABRIC_EXTRACTION_CHECKER", raising=False)
    passages = [{"id": "p1", "role": "primary", "start": 0, "end": len(TEXT), "text": TEXT}]
    located = {"subject": SUBJECT, "passages": passages, "items": [_item(kind)]}
    answer = biocheck(located)
    assert answer["status"] == 200, answer
    assert answer["body"]["items"][0]["fields"]["locator"]["result"] == "match"
    assert checks._verdict(answer["body"], located) is None, answer["body"]["items"][0]
    if len(answer["body"]["items"][0]["fields"]) > 1:
        partial = copy.deepcopy(answer["body"])
        name = next(n for n in partial["items"][0]["fields"] if n != "locator")
        partial["items"][0]["fields"].pop(name)
        assert checks._verdict(partial, located) == "invalid_response"
    misquoted = copy.deepcopy(located)
    misquoted["items"][0]["quote"] = "other"
    negative = biocheck(misquoted)
    assert negative["status"] == 200, negative
    item = negative["body"]["items"][0]
    assert item["status"] == "inconsistent" and item["fields"]["locator"]["result"] == "mismatch", item
    assert checks._verdict(negative["body"], misquoted) is None, item
    forged = copy.deepcopy(negative["body"])
    forged["items"][0]["status"] = "consistent"
    assert checks._verdict(forged, misquoted) == "invalid_response"
