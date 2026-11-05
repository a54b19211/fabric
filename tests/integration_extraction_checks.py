"""v3 6.2 (T-29 P2) on real PostgreSQL: extraction-consistency checks of processing products, registered with the
products, built from the fixed artifact, sent to a BioCheck stand-in over real HTTP semantics (MockTransport, no
network), kept append-only and read back with the source permission. Failures are unchecked, never a pass."""

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text

from biz_rules.history_models import HistoryError
from db import extraction_checks as checks
from integration_index_publication import add_document, pg, pg_engine, profile, rows  # noqa: F401  (fixtures)



class FakeBioCheck:
    """POST /v1/extraction-consistency as the contract defines it: every item answered consistent."""

    def __init__(self, status=200, fail=False, body=None):
        self.status, self.fail, self.body, self.requests = status, fail, body, []

    def handler(self, request):
        assert request.url.path == "/v1/extraction-consistency"
        assert request.headers["Authorization"] == "Bearer test-token"
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.fail:
            raise httpx.ConnectTimeout("synthetic timeout")
        if self.status != 200:
            return httpx.Response(self.status, json={"error_code": "service_unavailable", "message": "x"})
        if self.body is not None:
            return httpx.Response(200, json=self.body)
        items = [{"id": i["id"], "kind": i["kind"], "status": "consistent",
                  "fields": {"locator": {"result": "match"}, "quote": {"result": "match"}}} for i in payload["items"]]
        return httpx.Response(200, json={"contract_version": checks.CONTRACT, "checker_version": "biocheck.extraction_rules.v3",
                                         "subject": payload["subject"], "items": items,
                                         "counts": {"consistent": len(items), "inconsistent": 0, "unchecked": 0},
                                         "llm_calls": []})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_BIOCHECK_URL", "http://biocheck.invalid")
    monkeypatch.setenv("DATAFABRIC_BIOCHECK_TOKEN", "test-token")


def document():
    version = add_document("NCT01234567", brief_summary="Registered as NCT01234567 (PMID: 31234567). The drug was "
                                                        "tested in mice and in vitro before the first human dose.")
    profile()
    return version


def drain(fake):
    states = []
    while True:
        state = checks.process_once(client=fake.client())["state"]
        if state == "idle":
            return states
        states.append(state)


def stored(pg, version, producer="identifiers"):
    """Result rows of one producer (the profile of these documents may have no locators: a no_items marker)."""
    return rows(pg, "SELECT producer,status,error_code,checker_version,request_sha256,text_sha256 FROM "
                    "derived_extraction_checks WHERE version_id=:v AND producer=:p ORDER BY id",
                {"v": version, "p": producer})


def test_tasks_are_registered_in_the_same_transaction_as_the_products(pg):
    version = document()
    tasks = rows(pg, "SELECT producer,state FROM derived_extraction_check_tasks WHERE version_id=:v ORDER BY producer",
                 {"v": version})
    assert [t["producer"] for t in tasks] == ["identifiers", "profile"] and {t["state"] for t in tasks} == {"queued"}
    assert rows(pg, "SELECT count(*) AS n FROM derived_identifiers WHERE version_id=:v", {"v": version})[0]["n"] > 0
    # a product without a task is caught up by the backfill
    with pg.begin() as conn:
        conn.execute(text("DELETE FROM derived_extraction_check_tasks"))
    assert checks.backfill() == 2


def test_the_request_is_built_from_the_fixed_artifact_and_the_result_kept(pg, env):
    version = document()
    fake = FakeBioCheck()
    assert set(drain(fake)) == {"done"}
    (clean,) = rows(pg, "SELECT text,text_sha256,processor_version FROM derived_clean_text WHERE version_id=:v", {"v": version})
    assert fake.requests, "nothing was asked"
    for req in fake.requests:
        assert req["subject"]["artifact"] == {"processor_version": clean["processor_version"], "text_sha256": clean["text_sha256"]}
        assert req["subject"]["is_demo"] is True and req["subject"]["version_id"] == version
        for p in req["passages"]:  # contiguous slices of the fixed artifact, bounded
            assert clean["text"][p["start"]:p["end"]] == p["text"]
        assert sum(len(p["text"]) for p in req["passages"]) <= 20000 and len(req["items"]) <= 50
        for i in req["items"]:
            assert clean["text"][i["locator"]["start"]:i["locator"]["end"]] == i["quote"] or i["kind"] == "annotation"
    kinds = {i["kind"] for r in fake.requests for i in r["items"]}
    assert "identifier" in kinds
    results = stored(pg, version)
    assert results and {r["status"] for r in results} == {"completed"}
    assert {r["checker_version"] for r in results} == {"biocheck.extraction_rules.v3"}
    got = checks.for_versions("external_archive", True, [version], "current", datetime.now(timezone.utc))["versions"][version]
    assert got["identifiers"]["state"] == "checked" and got["identifiers"]["counts"]["consistent"] > 0


def test_only_the_same_full_request_and_checker_is_reused(pg, env, monkeypatch):
    version = document()
    first = FakeBioCheck()
    drain(first)
    sent = len(first.requests)

    def requeue():
        with pg.begin() as conn:
            conn.execute(text("UPDATE derived_extraction_check_tasks SET state='queued',next_check_at=clock_timestamp()"))

    requeue()
    again = FakeBioCheck()
    drain(again)
    assert again.requests == []  # same request, same checker: reused, nothing sent
    monkeypatch.setenv("DATAFABRIC_EXTRACTION_CHECKER", "biocheck.extraction_rules.v4")
    assert checks.backfill() == 2  # an upgrade queues every product again
    newer = FakeBioCheck()
    drain(newer)
    assert len(newer.requests) >= 1  # another checker version: asked again (the v3 answer is not adopted)
    got = checks.for_versions("external_archive", True, [version], "current", datetime.now(timezone.utc))
    assert got["versions"][version]["identifiers"]["state"] == "unchecked"
    # another product content (one more identifier) changes the request: never served by the old answer
    monkeypatch.delenv("DATAFABRIC_EXTRACTION_CHECKER")
    with pg.begin() as conn:
        row = conn.execute(text("SELECT * FROM derived_identifiers WHERE version_id=:v LIMIT 1"), {"v": version}).mappings().one()
        conn.execute(text("INSERT INTO derived_identifiers(version_id,processor_version,text_processor_version,kind,value,"
                          "start_offset,end_offset) VALUES (:v,:pv,:tpv,:k,:val,:s,:e)"),
                     {"v": version, "pv": row["processor_version"], "tpv": row["text_processor_version"], "k": row["kind"],
                      "val": row["value"] + "9", "s": row["start_offset"], "e": row["end_offset"]})
    requeue()
    changed = FakeBioCheck()
    drain(changed)
    assert any(r["items"][0]["kind"] == "identifier" for r in changed.requests)


def test_a_revoked_source_is_unchecked_without_any_call_or_reuse(pg, env):
    version = document()
    drain(FakeBioCheck())
    with pg.begin() as conn:
        conn.execute(text("UPDATE derived_extraction_check_tasks SET state='queued',next_check_at=clock_timestamp()"))
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"))
    fake = FakeBioCheck()
    drain(fake)
    assert fake.requests == []
    assert {r["error_code"] for r in stored(pg, version) if r["status"] == "unchecked"} == {"source_unreadable"}
    with pytest.raises(HistoryError) as exc:
        checks.for_versions("external_archive", True, [version], "current", datetime.now(timezone.utc))
    assert exc.value.status == 403


@pytest.mark.parametrize("fake", [FakeBioCheck(status=503), FakeBioCheck(fail=True)])
def test_a_failed_call_is_unchecked_and_retried(pg, env, fake):
    version = document()
    assert "waiting" in drain(fake)
    assert {r["error_code"] for r in stored(pg, version)} == {"service_unavailable"}
    got = checks.for_versions("external_archive", True, [version], "current", datetime.now(timezone.utc))["versions"][version]
    assert got["identifiers"]["state"] == "unchecked" and got["identifiers"]["reason"] == "service_unavailable"
    with pg.begin() as conn:
        conn.execute(text("UPDATE derived_extraction_check_tasks SET next_check_at=clock_timestamp()"))
    drain(FakeBioCheck())
    got = checks.for_versions("external_archive", True, [version], "current", datetime.now(timezone.utc))["versions"][version]
    assert got["identifiers"]["state"] == "checked"


def test_a_contract_rejection_blocks_and_an_invalid_answer_is_unchecked(pg, env):
    version = document()
    assert "blocked" in drain(FakeBioCheck(status=400))
    assert {r["error_code"] for r in stored(pg, version)} == {"contract_rejected"}
    with pg.begin() as conn:
        conn.execute(text("UPDATE derived_extraction_check_tasks SET state='queued',attempts=0,next_check_at=clock_timestamp()"))
    drain(FakeBioCheck(body={"contract_version": checks.CONTRACT, "checker_version": "biocheck.extraction_rules.v3", "items": []}))
    assert "invalid_response" in {r["error_code"] for r in stored(pg, version)}


def test_a_changed_artifact_is_unchecked_and_results_are_append_only(pg, env):
    version = document()
    with pg.begin() as conn:
        clean = conn.execute(text("SELECT * FROM derived_clean_text WHERE version_id=:v"), {"v": version}).mappings().one()
        cols = [c for c in clean if c not in ("attempt", "text", "created_at")]
        conn.execute(text("INSERT INTO derived_clean_text (" + ",".join(cols) + ",attempt,text) SELECT "
                          + ",".join(cols) + ",2,text||' tampered' FROM derived_clean_text WHERE version_id=:v"),
                     {"v": version})
    fake = FakeBioCheck()
    drain(fake)
    assert fake.requests == [] and {r["error_code"] for r in stored(pg, version)} == {"artifact_changed"}
    with pg.begin() as conn:
        conn.execute(text("UPDATE derived_extraction_checks SET status='completed'"))
        conn.execute(text("DELETE FROM derived_extraction_checks"))
    assert {r["status"] for r in stored(pg, version)} == {"unchecked"}
