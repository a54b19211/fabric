"""Extraction consistency of processing products (v3 6.2, T-29 P2): BioCheck checks whether what Fabric extracted
corresponds to the fixed text it was extracted from - never whether a fact is true.

- A task per (version, producer, producer version, clean-text processor) is registered in the same transaction
  as the products it checks (db.clean_stage); a backfill registers tasks a product somehow lacks.
- The worker processes tasks outside any transaction around the HTTP call: it re-checks the source permission,
  reads the fixed artifact again and verifies its digest, builds bounded requests (contiguous passages of that
  artifact, <= 8 passages, <= 20,000 characters, <= 50 items), and calls POST /v1/extraction-consistency.
- A request already answered (completed) with the same full canonical request digest and the expected checker
  version, for the same demo/real kind, is reused - after the permission check - instead of being sent again.
  The digest covers the subject, the artifact, every passage and item, and the producer versions.
- Every outcome is kept append-only (derived_extraction_checks): completed with BioCheck's per-item results, or
  unchecked with a reason (source_unreadable, artifact_changed, service_unavailable, invalid_response,
  contract_rejected). An unchecked outcome is never a pass; a failed call is retried with backoff.
Producers checked: identifiers (identifier items: the producer's value at its offsets) and profile annotations
(experiment-type locators; BioCheck v3 checks their locator and quote, the type judgement stays unsupported)."""

import hashlib
import json
import os
import time
from uuid import uuid4

import httpx
from sqlalchemy import text

from biz_rules.history_models import HistoryError
from db import history

CONTRACT = "extraction_consistency.v1"
LEASE_SECONDS = 120
WINDOW, MAX_ITEMS, MAX_PASSAGES, MAX_CHARS = 200, 50, 8, 20000
PRODUCERS = ("identifiers", "profile")


def producer_version(name):
    from processing import identifiers, profile
    return {"identifiers": identifiers.PROCESSOR_VERSION, "profile": profile.PROCESSOR_VERSION}[name]


def expected_checker():
    return os.environ.get("DATAFABRIC_EXTRACTION_CHECKER", "biocheck.extraction_rules.v3")


def configured():
    return bool(os.environ.get("DATAFABRIC_BIOCHECK_URL") and os.environ.get("DATAFABRIC_BIOCHECK_TOKEN"))


def enqueue(conn, versions):
    """Register the check tasks of freshly written products, in the caller's transaction.
    versions: [(version_id, clean-text processor version)]. Returns the number of new tasks."""
    rows = [{"v": v, "p": p, "pv": producer_version(p), "tpv": tpv} for v, tpv in versions for p in PRODUCERS]
    if not rows:
        return 0
    result = conn.execute(text("""
        INSERT INTO derived_extraction_check_tasks(version_id,producer,producer_version,text_processor_version)
        VALUES (:v,:p,:pv,:tpv) ON CONFLICT DO NOTHING
    """), rows)
    return result.rowcount


def backfill(batch_size=200):
    """Reliable catch-up: tasks for products that have none (written before this existed, or by another path)."""
    with history.history_engine().begin() as conn:
        n = conn.execute(text("""
            INSERT INTO derived_extraction_check_tasks(version_id,producer,producer_version,text_processor_version)
            SELECT version_id,producer,pv,tpv FROM (
              SELECT DISTINCT version_id,'identifiers' AS producer,processor_version AS pv,text_processor_version AS tpv
                FROM derived_identifiers WHERE processor_version=:ipv
              UNION
              SELECT version_id,'profile',processor_version,text_processor_version
                FROM derived_profiles WHERE processor_version=:ppv
                  AND experiment_types IS NOT NULL AND experiment_types<>'[]'::jsonb) x
            WHERE NOT EXISTS (SELECT 1 FROM derived_extraction_check_tasks t WHERE t.version_id=x.version_id
                              AND t.producer=x.producer AND t.producer_version=x.pv AND t.text_processor_version=x.tpv)
            LIMIT :n ON CONFLICT DO NOTHING
        """), {"ipv": producer_version("identifiers"), "ppv": producer_version("profile"), "n": batch_size}).rowcount
    return n


def _claim():
    with history.history_engine().begin() as conn:
        row = conn.execute(text("""
            SELECT * FROM derived_extraction_check_tasks
            WHERE state IN ('queued','waiting') AND next_check_at<=clock_timestamp()
              AND (lease_until IS NULL OR lease_until<clock_timestamp())
            ORDER BY next_check_at,version_id FOR UPDATE SKIP LOCKED LIMIT 1
        """)).mappings().first()
        if row is None:
            return None
        token = uuid4().hex
        conn.execute(text("""
            UPDATE derived_extraction_check_tasks SET lease_token=:t,
              lease_until=clock_timestamp()+make_interval(secs=>:lease),updated_at=clock_timestamp()
            WHERE version_id=:v AND producer=:p AND producer_version=:pv AND text_processor_version=:tpv
        """), {"t": token, "lease": LEASE_SECONDS, "v": row["version_id"], "p": row["producer"],
               "pv": row["producer_version"], "tpv": row["text_processor_version"]})
        return dict(row, lease_token=token)


def _finish(task, state, *, delay=0, error=None):
    attempts = task["attempts"] + 1 if error else task["attempts"]
    if error and attempts >= 6:
        state = "blocked"
    with history.history_engine().begin() as conn:
        conn.execute(text("""
            UPDATE derived_extraction_check_tasks SET state=:s,attempts=:a,error=CAST(:e AS jsonb),lease_token=NULL,
              lease_until=NULL,next_check_at=clock_timestamp()+make_interval(secs=>:d),updated_at=clock_timestamp()
            WHERE version_id=:v AND producer=:p AND producer_version=:pv AND text_processor_version=:tpv
              AND lease_token=:t
        """), {"s": state, "a": attempts, "e": json.dumps(error) if error else None, "d": delay,
               "v": task["version_id"], "p": task["producer"], "pv": task["producer_version"],
               "tpv": task["text_processor_version"], "t": task["lease_token"]})
    return state


def _record(conn, task, version, artifact_sha, request_sha, status, *, items=(), counts=None, checker=None,
            error_code=None, calls=None):
    conn.execute(text("""
        INSERT INTO derived_extraction_checks(version_id,source_id,is_demo,producer,producer_version,
          text_processor_version,text_sha256,contract_version,checker_version,request_sha256,status,error_code,
          items,counts,calls)
        VALUES (:v,:source,:demo,:p,:pv,:tpv,:sha,:contract,:checker,:req,:status,:err,CAST(:items AS jsonb),
                CAST(:counts AS jsonb),CAST(:calls AS jsonb))
    """), {"v": task["version_id"], "source": version["source_id"], "demo": version["is_demo"],
           "p": task["producer"], "pv": task["producer_version"], "tpv": task["text_processor_version"],
           "sha": artifact_sha or "", "contract": CONTRACT, "checker": checker, "req": request_sha, "status": status,
           "err": error_code, "items": json.dumps(list(items), ensure_ascii=False),
           "counts": json.dumps(counts) if counts is not None else None,
           "calls": json.dumps(calls) if calls is not None else None})


def _items(conn, task, full):
    """The products of the task as contract items (absolute offsets in the fixed artifact)."""
    out = []
    if task["producer"] == "identifiers":
        for n, r in enumerate(conn.execute(text("""
                SELECT kind,value,start_offset,end_offset FROM derived_identifiers WHERE version_id=:v
                  AND processor_version=:pv AND text_processor_version=:tpv ORDER BY start_offset,kind,value
                """), {"v": task["version_id"], "pv": task["producer_version"],
                       "tpv": task["text_processor_version"]}).mappings(), 1):
            quote = full[r["start_offset"]:r["end_offset"]]
            out.append({"id": f"i{n}", "kind": "identifier", "start": r["start_offset"], "end": r["end_offset"],
                        "quote": quote, "producer": {"name": "datafabric.identifiers", "version": task["producer_version"]},
                        "fields": {"scheme": r["kind"][:40], "identifier": {"state": "stated", "value": r["value"][:300]}}})
    else:
        types = conn.execute(text("""
            SELECT experiment_types FROM derived_profiles WHERE version_id=:v AND processor_version=:pv
              AND text_processor_version=:tpv ORDER BY created_at DESC LIMIT 1
        """), {"v": task["version_id"], "pv": task["producer_version"],
               "tpv": task["text_processor_version"]}).scalar() or []
        n = 0
        for t in types:
            for loc in t.get("locators") or []:
                n += 1
                out.append({"id": f"a{n}", "kind": "annotation", "start": loc["start"], "end": loc["end"],
                            "quote": loc.get("text") or "", "fields": {"label": str(t.get("type"))[:80]},
                            "producer": {"name": "datafabric.profile", "version": task["producer_version"]}})
    return out


def _requests(subject, full, items):
    """Bounded requests: each item's contiguous window [start-200, end+200) of the artifact, windows merged,
    at most 8 passages / 20,000 characters / 50 items per request. Items whose offsets fall outside the artifact
    or whose quote is empty or too long are returned apart (unbuildable), never sent as if checkable."""
    unbuildable = [i for i in items if not (0 <= i["start"] < i["end"] <= len(full)) or not 0 < len(i["quote"]) <= 300]
    usable = sorted((i for i in items if i not in unbuildable), key=lambda i: (i["start"], i["end"]))
    batches, current = [], []

    def windows(group):
        spans = []
        for i in group:
            a, b = max(0, i["start"] - WINDOW), min(len(full), i["end"] + WINDOW)
            if spans and a <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        return spans

    for item in usable:
        trial = current + [item]
        spans = windows(trial)
        if current and (len(trial) > MAX_ITEMS or len(spans) > MAX_PASSAGES or sum(b - a for a, b in spans) > MAX_CHARS):
            batches.append(current)
            current = [item]
        else:
            current = trial
    if current:
        batches.append(current)
    out = []
    for group in batches:
        spans = windows(group)
        passages = [{"id": f"p{n}", "role": "primary", "start": a, "end": b, "text": full[a:b]}
                    for n, (a, b) in enumerate(spans, 1)]

        def holder(i):
            return next(p["id"] for p in passages if p["start"] <= i["start"] and i["end"] <= p["end"])

        body_items = []
        for i in group:
            span = {"passage": holder(i), "start": i["start"], "end": i["end"]}
            fields = json.loads(json.dumps(i["fields"]))
            if i["kind"] == "identifier":
                fields["identifier"]["span"] = span
            body_items.append({"id": i["id"], "kind": i["kind"], "producer": i["producer"], "locator": span,
                               "quote": i["quote"], "fields": fields})
        out.append({"subject": subject, "passages": passages, "items": body_items})
    return out, unbuildable


def canonical(body):
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _valid_response(body, request):
    if not isinstance(body, dict) or body.get("contract_version") != CONTRACT or not body.get("checker_version"):
        return False
    sent = {i["id"] for i in request["items"]}
    got = body.get("items") or []
    return ({i.get("id") for i in got} == sent and len(got) == len(sent)
            and all(i.get("status") in ("consistent", "inconsistent", "unchecked") for i in got))


def process_once(*, client=None):
    task = _claim()
    if task is None:
        return {"state": "idle"}
    with history.history_engine().begin() as conn:
        version = conn.execute(text(
            "SELECT version_id,source_id,source_record_id,is_demo,payload->>'raw_sha256' AS raw_sha256 "
            "FROM history_versions WHERE version_id=:v"), {"v": task["version_id"]}).mappings().first()
        if version is None:
            return {"state": _finish(task, "blocked", error={"code": "version_missing"})}
        try:
            history._source(conn, version["source_id"])
        except HistoryError as exc:
            if exc.status != 403:
                raise
            _record(conn, task, version, None, "0" * 64, "unchecked", error_code="source_unreadable")
            return {"state": _finish(task, "waiting", delay=3600)}  # revoked: no HTTP, no reuse; looked at again later
        art = conn.execute(text(
            "SELECT text,text_sha256 FROM derived_clean_text WHERE version_id=:v AND processor_version=:tpv "
            "AND status='parsed' ORDER BY attempt DESC LIMIT 1"),
            {"v": task["version_id"], "tpv": task["text_processor_version"]}).mappings().first()
        full = (art or {}).get("text") or ""
        if art is None or hashlib.sha256(full.encode()).hexdigest() != art["text_sha256"]:
            _record(conn, task, version, (art or {}).get("text_sha256"), "0" * 64, "unchecked",
                    error_code="artifact_changed")
            return {"state": _finish(task, "blocked")}
        items = _items(conn, task, full)
    subject = {"source_id": version["source_id"], "record_id": version["source_record_id"][:2000],
               "version_id": version["version_id"].strip(), "raw_sha256": version["raw_sha256"] or "0" * 64,
               "is_demo": bool(version["is_demo"]),
               "artifact": {"processor_version": task["text_processor_version"], "text_sha256": art["text_sha256"]}}
    requests, unbuildable = _requests(subject, full, items)
    if unbuildable:
        with history.history_engine().begin() as conn:
            _record(conn, task, version, art["text_sha256"], hashlib.sha256(canonical(
                [{k: i[k] for k in ("id", "kind", "start", "end")} for i in unbuildable]).encode()).hexdigest(),
                "unchecked", error_code="item_not_buildable",
                items=[{"id": i["id"], "kind": i["kind"], "status": "unchecked",
                        "reason": "offsets_outside_artifact_or_quote_unusable"} for i in unbuildable])
    owned = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
    failed = None
    try:
        for request in requests:
            request_sha = hashlib.sha256(canonical(request).encode()).hexdigest()
            with history.history_engine().begin() as conn:
                reused = conn.execute(text(
                    "SELECT 1 FROM derived_extraction_checks WHERE request_sha256=:r AND checker_version=:c "
                    "AND is_demo=:d AND status='completed' LIMIT 1"),
                    {"r": request_sha, "c": expected_checker(), "d": subject["is_demo"]}).first()
            if reused:
                continue  # the same full request was answered by this checker version: nothing new to ask
            started = time.monotonic()
            try:
                response = client.post(os.environ["DATAFABRIC_BIOCHECK_URL"].rstrip("/") + "/v1/extraction-consistency",
                                       json=request, headers={"Authorization": "Bearer " + os.environ["DATAFABRIC_BIOCHECK_TOKEN"]})
                status, body = response.status_code, (response.json() if response.content else None)
            except (httpx.HTTPError, ValueError):
                status, body = None, None
            calls = {"seconds": round(time.monotonic() - started, 3), "http_status": status}
            with history.history_engine().begin() as conn:
                if status == 200 and _valid_response(body, request):
                    _record(conn, task, version, art["text_sha256"], request_sha, "completed", items=body["items"],
                            counts=body.get("counts"), checker=body["checker_version"],
                            calls={**calls, "llm_calls": len(body.get("llm_calls") or [])})
                    continue
                code = ("contract_rejected" if status == 400 else "invalid_response" if status == 200
                        else "service_unavailable")
                _record(conn, task, version, art["text_sha256"], request_sha, "unchecked", error_code=code, calls=calls)
                failed = code
                break
    finally:
        if owned:
            client.close()
    if failed == "contract_rejected":
        return {"state": _finish(task, "blocked", error={"code": failed})}
    if failed:
        return {"state": _finish(task, "waiting", delay=min(3600, 30 * 2 ** task["attempts"]), error={"code": failed})}
    return {"state": _finish(task, "done")}


def for_versions(source_id, is_demo, version_ids, mode, cutoff):
    """The checks of visible versions: per producer, the latest completed outcome (per-item results and counts)
    and the latest unchecked reason. A version without a completed check is "unchecked" - never a pass."""
    import re

    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    eligible = history._eligibility(mode).replace("first_seen_at", "v.first_seen_at").replace(
        "proof_available_at", "v.proof_available_at")
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        history._source(conn, source_id)
        cutoff = history._cutoff(cutoff)
        if cutoff > history._now(conn):
            raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        visible = [r[0] for r in conn.execute(text(
            "SELECT v.version_id FROM history_versions v WHERE v.source_id=:s AND v.is_demo=:d AND v.version_id=ANY(:ids) "
            "AND " + eligible), {"s": source_id, "d": is_demo, "ids": list(version_ids), "cutoff": cutoff})]
        rows = conn.execute(text(
            "SELECT DISTINCT ON (version_id,producer,status) version_id,producer,producer_version,text_processor_version,"
            "text_sha256,checker_version,status,error_code,items,counts,created_at FROM derived_extraction_checks "
            "WHERE version_id=ANY(:ids) AND is_demo=:d ORDER BY version_id,producer,status,created_at DESC"),
            {"ids": visible, "d": is_demo}).mappings().all()
    out = {}
    for v in visible:
        mine = [r for r in rows if r["version_id"] == v]
        producers = {}
        for p in PRODUCERS:
            done = next((r for r in mine if r["producer"] == p and r["status"] == "completed"), None)
            bad = next((r for r in mine if r["producer"] == p and r["status"] == "unchecked"), None)
            producers[p] = {"state": "checked" if done else "unchecked",
                            "checker_version": done["checker_version"] if done else None,
                            "counts": done["counts"] if done else None,
                            "items": done["items"] if done else [],
                            "last_unchecked_reason": bad["error_code"] if bad else None}
        out[v.strip()] = producers
    return {"source_id": source_id, "is_demo": is_demo, "contract_version": CONTRACT, "versions": out}


def run_worker(*, stop, idle_seconds=5.0, backfill_every=60.0, rounds=None):
    last, n = 0.0, 0
    while not stop.is_set() and (rounds is None or n < rounds):
        n += 1
        if time.monotonic() - last >= backfill_every:
            last = time.monotonic()
            backfill()
        state = process_once()["state"]
        if state == "idle":
            stop.wait(idle_seconds)


if __name__ == "__main__":
    import signal
    import threading

    if not configured():
        print(json.dumps({"extraction_checks": {"state": "disabled"}}))
    else:
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        run_worker(stop=stop)
