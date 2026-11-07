"""Extraction consistency of processing products (v3 6.2, T-29 P2): BioCheck checks whether what Fabric extracted
corresponds to the fixed text it was extracted from - never whether a fact is true.

- A task per (version, producer, producer version, clean-text processor, checker version) is registered in the
  same transaction as the products it checks (db.clean_stage); backfill() registers what is missing, including
  every product when the expected checker version changes (an upgrade queues new work, old results stop counting).
- The worker runs one task as one run (run_id) of bounded batches, outside any transaction around the HTTP calls.
  Before every batch it renews its lease and re-checks the source permission (a revoked source gets no further
  material); each result is written only while the lease is still its own and the permission still holds,
  checked in the same transaction as the write - a late or orphaned response never publishes a result.
- A response is adopted only when its contract, checker version (the expected one), subject (exactly the request's),
  items (same ids and kinds, in order) and every item's status (derived from its own field results by the contract
  rule) all agree; anything else is unchecked "invalid_response" / "checker_mismatch", never completed, never a crash.
- A batch already answered for the same full canonical request, checker and demo kind is reused: a new completed
  row of this run points at it (reused_from), so a run always has one row per batch.
- Readback: per producer, the latest run for the current checker counts as checked only when every one of its
  batches completed; its items are all batches' items (plus unbuildable items as unchecked). Anything else -
  incomplete, failed, older checker - is "unchecked" with its reason; earlier runs stay as history.
Producers checked: identifiers (identifier items: the producer's value at its offsets) and profile annotations
(experiment-type locators; BioCheck v3 checks their locator and quote, the type judgement stays unsupported).
Each producer is checked under its own contract by its own checker (CHECKS_BY_PRODUCER): identifiers and profile
annotations by BioCheck /v1 (contract v1, checker v3), measurements by BioCheck /v3 (contract v3, checker v16). A
task waits for its producer's expected checker; a result is recorded under its producer's contract."""

import hashlib
import json
import os
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx
from sqlalchemy import text

from biz_rules.history_models import HistoryError
from extraction_contract import CHECKER_V3, CHECKER_V16, CONTRACT, CONTRACT_V3, canonical, verdict
from extraction_contract import derived_status as _derived_status
from db import history
from db.lease import LeaseQueue

LEASE_SECONDS = 120
WINDOW, MAX_ITEMS, MAX_PASSAGES, MAX_CHARS = 200, 50, 8, 20000
PRODUCERS = ("identifiers", "profile")


@dataclass(frozen=True)
class Check:
    """How one producer's products are checked: the contract, the BioCheck endpoint, and the checker expected
    (the environment variable that may name another one, and the default)."""

    contract: str
    path: str
    checker_env: str
    default_checker: str


_V1 = Check(CONTRACT, "/v1/extraction-consistency", "DATAFABRIC_EXTRACTION_CHECKER", CHECKER_V3)
# the explicit table: identifiers and annotations go to /v1, measurements to /v3 - never one for the other
CHECKS_BY_PRODUCER = {
    "identifiers": _V1,
    "profile": _V1,
    "measures": Check(CONTRACT_V3, "/v3/extraction-consistency", "DATAFABRIC_MEASURE_CHECKER", CHECKER_V16),
}
# Contract lengths (extraction_consistency.v1). A product longer than these is not sent cut to fit: a prefix of the
# producer's value is a different value, so the item is unbuildable and reads back unchecked.
LIMITS = {"quote": 300, "value": 300, "scheme": 40, "label": 80, "producer_version": 80,
          "source_id": 80, "record_id": 2000, "version_id": 128}
TASK_KEY = ("version_id", "producer", "producer_version", "text_processor_version", "checker_version")


def producer_version(name):
    from processing import identifiers, profile
    return {"identifiers": identifiers.PROCESSOR_VERSION, "profile": profile.PROCESSOR_VERSION}[name]


def expected_checker(producer="identifiers"):
    check = CHECKS_BY_PRODUCER[producer]
    return os.environ.get(check.checker_env, check.default_checker)


def contract_of(producer):
    return CHECKS_BY_PRODUCER[producer].contract


def configured():
    return bool(os.environ.get("DATAFABRIC_BIOCHECK_URL") and os.environ.get("DATAFABRIC_BIOCHECK_TOKEN"))


def enqueue(conn, versions):
    """Register the check tasks of freshly written products, in the caller's transaction.
    versions: [(version_id, clean-text processor version)]. Returns the number of new tasks."""
    rows = [{"v": v, "p": p, "pv": producer_version(p), "tpv": tpv, "c": expected_checker()}
            for v, tpv in versions for p in PRODUCERS]
    if not rows:
        return 0
    return conn.execute(text("""
        INSERT INTO derived_extraction_check_tasks(version_id,producer,producer_version,text_processor_version,
          checker_version) VALUES (:v,:p,:pv,:tpv,:c) ON CONFLICT DO NOTHING
    """), rows).rowcount


def backfill(batch_size=200):
    """Reliable catch-up: tasks for products that have none for the expected checker version - written before this
    existed, by another path, or checked by an older checker (an upgrade re-queues everything)."""
    with history.history_engine().begin() as conn:
        return conn.execute(text("""
            INSERT INTO derived_extraction_check_tasks(version_id,producer,producer_version,text_processor_version,
              checker_version)
            SELECT version_id,producer,pv,tpv,:c FROM (
              SELECT DISTINCT version_id,'identifiers' AS producer,processor_version AS pv,text_processor_version AS tpv
                FROM derived_identifiers WHERE processor_version=:ipv
              UNION
              SELECT version_id,'profile',processor_version,text_processor_version
                FROM derived_profiles WHERE processor_version=:ppv
                  AND experiment_types IS NOT NULL AND experiment_types<>'[]'::jsonb) x
            WHERE NOT EXISTS (SELECT 1 FROM derived_extraction_check_tasks t WHERE t.version_id=x.version_id
                              AND t.producer=x.producer AND t.producer_version=x.pv AND t.text_processor_version=x.tpv
                              AND t.checker_version=:c)
            LIMIT :n ON CONFLICT DO NOTHING
        """), {"ipv": producer_version("identifiers"), "ppv": producer_version("profile"), "c": expected_checker(),
               "n": batch_size}).rowcount


def enqueue_measures(conn, version_id, producer_identity, text_processor_version):
    """The check task of one measurement run's readings (producer "measures", the run's producer identity), in the
    caller's transaction: registered, or queued again when its readings changed after it was done (a retried window,
    a relocated reading). A blocked task stays blocked."""
    return conn.execute(text("""
        INSERT INTO derived_extraction_check_tasks(version_id,producer,producer_version,text_processor_version,
          checker_version) VALUES (:v,'measures',:pv,:tpv,:c)
        ON CONFLICT (version_id,producer,producer_version,text_processor_version,checker_version) DO UPDATE
          SET state='queued',next_check_at=clock_timestamp(),updated_at=clock_timestamp()
          WHERE derived_extraction_check_tasks.state='done'
    """), {"v": version_id, "pv": producer_identity, "tpv": text_processor_version,
           "c": expected_checker("measures")}).rowcount


def backfill_measures(batch_size=200):
    """Tasks for measurement readings that have none for the expected measurements checker: an upgrade of that
    checker queues every reading again (their objects of the older checker stop being current)."""
    with history.history_engine().begin() as conn:
        return conn.execute(text("""
            INSERT INTO derived_extraction_check_tasks(version_id,producer,producer_version,text_processor_version,
              checker_version)
            SELECT DISTINCT version_id,'measures',producer_identity,text_processor_version,:c
              FROM derived_measure_readings r
             WHERE NOT EXISTS (SELECT 1 FROM derived_extraction_check_tasks t WHERE t.version_id=r.version_id
                                 AND t.producer='measures' AND t.producer_version=r.producer_identity
                                 AND t.text_processor_version=r.text_processor_version AND t.checker_version=:c)
             LIMIT :n ON CONFLICT DO NOTHING
        """), {"c": expected_checker("measures"), "n": batch_size}).rowcount


def _key(task):
    return {k: task[k] for k in TASK_KEY}


TASKS = LeaseQueue("derived_extraction_check_tasks", TASK_KEY)

_WHERE = ("version_id=:version_id AND producer=:producer AND producer_version=:producer_version "
          "AND text_processor_version=:text_processor_version AND checker_version=:checker_version")


def _claim():
    """The next due task waiting for its own producer's expected checker."""
    params, wanted = {}, []
    for n, producer in enumerate(CHECKS_BY_PRODUCER):
        params[f"p{n}"], params[f"c{n}"] = producer, expected_checker(producer)
        wanted.append(f"(q.producer=:p{n} AND q.checker_version=:c{n})")
    with history.history_engine().begin() as conn:
        return TASKS.claim(conn, lease_seconds=LEASE_SECONDS, params=params,
                           where="q.state IN ('queued','waiting') AND (" + " OR ".join(wanted) + ") "
                                 "AND q.next_check_at<=clock_timestamp()",
                           order_by="q.next_check_at,q.version_id", also_set="updated_at=clock_timestamp()")


def _own(conn, task, renew=False):
    """True while the lease is still this worker's (locked for the rest of the transaction); renew extends it."""
    owned = TASKS.holds(conn, task, lock=True)
    if owned and renew:
        TASKS.renew(conn, task, LEASE_SECONDS)
    return owned


def _readable(conn, source_id):
    try:
        history._source(conn, source_id)
        return True
    except HistoryError as exc:
        if exc.status != 403:
            raise
        return False


def _finish(task, state, *, delay=0, error=None):
    attempts = task["attempts"] + 1 if error else task["attempts"]
    if error and attempts >= 6:
        state = "blocked"
    with history.history_engine().begin() as conn:
        TASKS.release(conn, task, "state=:s,attempts=:a,error=CAST(:e AS jsonb),"
                      "next_check_at=clock_timestamp()+make_interval(secs=>:d),updated_at=clock_timestamp()",
                      {"s": state, "a": attempts, "e": json.dumps(error) if error else None, "d": delay},
                      unexpired=False)
    return state


def _record(conn, task, version, run, artifact_sha, request_sha, status, *, items=(), counts=None, checker=None,
            error_code=None, calls=None, reused_from=None):
    conn.execute(text("""
        INSERT INTO derived_extraction_checks(version_id,source_id,is_demo,producer,producer_version,
          text_processor_version,text_sha256,contract_version,checker_version,request_sha256,status,error_code,
          items,counts,calls,run_id,run_batches,batch_index,reused_from)
        VALUES (:v,:source,:demo,:p,:pv,:tpv,:sha,:contract,:checker,:req,:status,:err,CAST(:items AS jsonb),
                CAST(:counts AS jsonb),CAST(:calls AS jsonb),:run,:batches,:index,:reused)
    """), {"v": task["version_id"], "source": version["source_id"], "demo": version["is_demo"],
           "p": task["producer"], "pv": task["producer_version"], "tpv": task["text_processor_version"],
           "sha": artifact_sha or "", "contract": contract_of(task["producer"]), "checker": checker, "req": request_sha, "status": status,
           "err": error_code, "items": json.dumps(list(items), ensure_ascii=False),
           "counts": json.dumps(counts) if counts is not None else None,
           "calls": json.dumps(calls) if calls is not None else None,
           "run": run["id"], "batches": run["batches"], "index": run["index"], "reused": reused_from})


def _readings(conn, task, sha):
    """The measurement readings of the task (its version, producer identity and artifact), in a fixed order."""
    return [dict(r) for r in conn.execute(text(
        "SELECT reading_id,item,passages FROM derived_measure_readings WHERE version_id=:v AND producer_identity=:pi "
        "AND text_processor_version=:tpv AND text_sha256=:sha ORDER BY reading_id"),
        {"v": task["version_id"], "pi": task["producer_version"], "tpv": task["text_processor_version"],
         "sha": sha}).mappings()]


def _items(conn, task, full):
    """The products of the task as contract items (absolute offsets in the fixed artifact)."""
    out = []
    if task["producer"] == "measures":
        return _readings(conn, task, hashlib.sha256(full.encode()).hexdigest())
    if task["producer"] == "identifiers":
        for n, r in enumerate(conn.execute(text("""
                SELECT kind,value,start_offset,end_offset FROM derived_identifiers WHERE version_id=:v
                  AND processor_version=:pv AND text_processor_version=:tpv ORDER BY start_offset,kind,value
                """), {"v": task["version_id"], "pv": task["producer_version"],
                       "tpv": task["text_processor_version"]}).mappings(), 1):
            quote = full[r["start_offset"]:r["end_offset"]]
            out.append({"id": f"i{n}", "kind": "identifier", "start": r["start_offset"], "end": r["end_offset"],
                        "quote": quote, "producer": {"name": "datafabric.identifiers", "version": task["producer_version"]},
                        "fields": {"scheme": r["kind"], "identifier": {"state": "stated", "value": r["value"]}}})
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
                            "quote": loc.get("text") or "", "fields": {"label": t.get("type")},
                            "producer": {"name": "datafabric.profile", "version": task["producer_version"]}})
    return out


def _fits(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def _subject_unusable(subject):
    """Why the subject cannot be sent as it is (None when it can): its ids are never cut to fit."""
    for name in ("source_id", "record_id", "version_id"):
        if not _fits(subject[name], LIMITS[name]):
            return f"subject_{name}_exceeds_contract"
    return None


def _unusable(item, full):
    """Why an item cannot be sent as the producer stated it (None when it can)."""
    if not (isinstance(item["start"], int) and isinstance(item["end"], int) and 0 <= item["start"] < item["end"] <= len(full)):
        return "offsets_outside_artifact"
    if not _fits(item["quote"], LIMITS["quote"]):
        return "quote_exceeds_contract"
    if not _fits(item["producer"]["version"], LIMITS["producer_version"]):
        return "producer_version_exceeds_contract"
    f = item["fields"]
    if item["kind"] == "identifier":
        if not _fits(f["scheme"], LIMITS["scheme"]):
            return "scheme_exceeds_contract"
        if not _fits(f["identifier"]["value"], LIMITS["value"]):
            return "value_exceeds_contract"
    elif item["kind"] == "annotation" and not _fits(f["label"], LIMITS["label"]):
        return "label_exceeds_contract"
    return None


def _requests(subject, full, items):
    """Bounded requests: each item's contiguous window [start-200, end+200) of the artifact, windows merged,
    at most 8 passages / 20,000 characters / 50 items per request. Items that cannot be sent exactly as produced
    (offsets outside the artifact, a quote / value / scheme / label / version over the contract length, or a
    subject over it) are returned apart with the reason (unbuildable), never cut to fit or sent as if checkable."""
    whole = _subject_unusable(subject)
    unbuildable = []
    for i in items:
        why = whole or _unusable(i, full)
        if why:
            unbuildable.append({**i, "reason": why})
    left = {i["id"] for i in unbuildable}
    usable = sorted((i for i in items if i["id"] not in left), key=lambda i: (i["start"], i["end"]))
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


def _measure_requests(subject, readings):
    """One contract-v3 request per reading: the strict item and passages it was built with at extraction time."""
    return [{"contract_version": contract_of("measures"), "subject": subject, "passages": r["passages"],
             "items": [{"id": "m1", **r["item"]}]} for r in readings], []


def _build(work):
    """(requests, unbuildable) of the task: per producer."""
    if work.task["producer"] == "measures":
        work.run["reading_ids"] = [r["reading_id"] for r in work.items]
        return _measure_requests(_subject(work), work.items)
    return _requests(_subject(work), work.full, work.items)


def _publish(work):
    """After every batch of a measures run completed: its objects, while the lease is still this worker's and the
    permission still holds. None, LEASE_LOST or "source_unreadable"."""
    from db import measurements

    with history.history_engine().begin() as conn:
        if not _own(conn, work.task):
            return LEASE_LOST
        if not _readable(conn, work.version["source_id"]):
            return "source_unreadable"
        measurements.publish(conn, work.task, work.run["id"], work.run["reading_ids"])
    return None


def _verdict(body, request, producer="identifiers"):
    """None when the response may be adopted, else the reason it may not (the shared contract's strict rules
    for the checker this deployment expects for the producer)."""
    return verdict(body, request, expected_checker(producer))


LEASE_LOST = "lease_lost"


@dataclass
class _Work:
    """One run of one task: the fixed version, its artifact (row and text) and the products as contract items."""

    task: dict
    run: dict
    version: dict
    art: dict
    full: str
    items: list


def _load(task, run):
    """(work, None), or (None, result) when the task ends before anything is built: the version is missing, its
    source is revoked (nothing sent, nothing reused) or its artifact changed."""
    with history.history_engine().begin() as conn:
        version = conn.execute(text(
            "SELECT version_id,source_id,source_record_id,is_demo,payload->>'raw_sha256' AS raw_sha256 "
            "FROM history_versions WHERE version_id=:v"), {"v": task["version_id"]}).mappings().first()
        if version is None:
            return None, {"state": _finish(task, "blocked", error={"code": "version_missing"})}
        if not _readable(conn, version["source_id"]):
            _record(conn, task, version, run, None, "0" * 64, "unchecked", error_code="source_unreadable")
            return None, {"state": _finish(task, "waiting", delay=3600)}
        art = conn.execute(text(
            "SELECT text,text_sha256 FROM derived_clean_text WHERE version_id=:v AND processor_version=:tpv "
            "AND status='parsed' ORDER BY attempt DESC LIMIT 1"),
            {"v": task["version_id"], "tpv": task["text_processor_version"]}).mappings().first()
        full = (art or {}).get("text") or ""
        if art is None or hashlib.sha256(full.encode()).hexdigest() != art["text_sha256"]:
            _record(conn, task, version, run, (art or {}).get("text_sha256"), "0" * 64, "unchecked",
                    error_code="artifact_changed")
            return None, {"state": _finish(task, "blocked")}
        return _Work(task, run, dict(version), dict(art), full, _items(conn, task, full)), None


def _subject(work):
    version = work.version
    return {"source_id": version["source_id"], "record_id": version["source_record_id"],
            "version_id": version["version_id"].strip(), "raw_sha256": version["raw_sha256"] or "0" * 64,
            "is_demo": bool(version["is_demo"]),
            "artifact": {"processor_version": work.task["text_processor_version"],
                         "text_sha256": work.art["text_sha256"]}}


def _record_unsent(work, requests, unbuildable):
    """Write what is known before any batch is sent: "no_items" (then the task is done) or the unbuildable items.
    Returns the final result when the task ends here, else None."""
    task, sha = work.task, work.art["text_sha256"]
    if not requests and not unbuildable:
        with history.history_engine().begin() as conn:
            if not _own(conn, task):
                return {"state": LEASE_LOST}
            _record(conn, task, work.version, work.run, sha, "0" * 64, "no_items",
                    checker=expected_checker(task["producer"]), error_code="no_items")
        return {"state": _finish(task, "done")}  # nothing to check: read back as "no_items", never as checked
    if unbuildable:
        with history.history_engine().begin() as conn:
            if not _own(conn, task):
                return {"state": LEASE_LOST}
            _record(conn, task, work.version, work.run, sha, hashlib.sha256(canonical(
                [{k: i[k] for k in ("id", "kind", "start", "end")} for i in unbuildable]).encode()).hexdigest(),
                "unchecked", error_code="item_not_buildable",
                items=[{"id": i["id"], "kind": i["kind"], "status": "unchecked",
                        "reason": i["reason"]} for i in unbuildable])
    return None


def _before_batch(work, request_sha):
    """Renew the lease and re-check the permission before a batch; reuse the answer of the same full request.
    Returns None (send it), "reused" (recorded), LEASE_LOST or "source_unreadable" (recorded)."""
    task = work.task
    with history.history_engine().begin() as conn:
        if not _own(conn, task, renew=True):
            return LEASE_LOST
        if not _readable(conn, work.version["source_id"]):
            _record(conn, task, work.version, work.run, work.art["text_sha256"], request_sha, "unchecked",
                    error_code="source_unreadable")
            return "source_unreadable"
        prior = conn.execute(text(
            "SELECT id,items,counts,checker_version FROM derived_extraction_checks WHERE request_sha256=:r "
            "AND checker_version=:c AND is_demo=:d AND status='completed' ORDER BY id LIMIT 1"),
            {"r": request_sha, "c": expected_checker(task["producer"]),
             "d": bool(work.version["is_demo"])}).mappings().first()
        if prior:  # the same full request answered by this checker: this run's batch points at it
            _record(conn, task, work.version, work.run, work.art["text_sha256"], request_sha, "completed",
                    items=prior["items"], counts=prior["counts"], checker=prior["checker_version"],
                    calls={"reused": True}, reused_from=prior["id"])
            return "reused"
    return None


def _post(client, request, producer):
    """One call to BioCheck at the producer's endpoint, outside any transaction: (body or None, call facts,
    verdict or None)."""
    started = time.monotonic()
    try:
        response = client.post(os.environ["DATAFABRIC_BIOCHECK_URL"].rstrip("/") + CHECKS_BY_PRODUCER[producer].path,
                               json=request, headers={"Authorization": "Bearer " + os.environ["DATAFABRIC_BIOCHECK_TOKEN"]})
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
    except httpx.HTTPError:
        status, body = None, None
    calls = {"seconds": round(time.monotonic() - started, 3), "http_status": status}
    verdict = (_verdict(body, request, producer) if status == 200 else
               "contract_rejected" if status == 400 else "service_unavailable")
    return body, calls, verdict


def _after_batch(work, request_sha, body, calls, verdict):
    """Write a batch's answer while the lease is still this worker's and the permission still holds. Returns None
    (completed), LEASE_LOST (a late response: published by nobody) or the reason it was not adopted (recorded)."""
    task, sha = work.task, work.art["text_sha256"]
    with history.history_engine().begin() as conn:
        if not _own(conn, task):
            if task["producer"] == "measures":  # a late answer is kept as audit of the stale run, adopted by nobody
                _record(conn, task, work.version, work.run, sha, request_sha, "unchecked", error_code="lease_lost",
                        items=_audit_items(body), checker=_checker_of(body), calls=calls)
            return LEASE_LOST
        if not _readable(conn, work.version["source_id"]):
            verdict = "source_unreadable"  # revoked while the call ran: not adopted
        if verdict is None:
            _record(conn, task, work.version, work.run, sha, request_sha, "completed",
                    items=body["items"], counts=body.get("counts"), checker=body["checker_version"],
                    calls={**calls, "llm_calls": len(body.get("llm_calls") or [])})
            return None
        _record(conn, task, work.version, work.run, sha, request_sha, "unchecked", error_code=verdict,
                items=_audit_items(body) if task["producer"] == "measures" else (), checker=_checker_of(body),
                calls=calls)
        return verdict


def _checker_of(body):
    return body.get("checker_version") if isinstance(body, dict) else None


def _audit_items(body):
    """The items of an answer that is not adopted, kept with the unchecked row as its audit (measurements)."""
    items = body.get("items") if isinstance(body, dict) else None
    return items if isinstance(items, list) else []


def _send_batches(client, work, requests):
    """None when every batch completed, else LEASE_LOST or the failure that ended the run."""
    for index, request in enumerate(requests):
        work.run["index"] = index
        request_sha = hashlib.sha256(canonical(request).encode()).hexdigest()
        before = _before_batch(work, request_sha)
        if before == "reused":
            continue
        if before is not None:
            return before
        body, calls, verdict = _post(client, request, work.task["producer"])
        after = _after_batch(work, request_sha, body, calls, verdict)
        if after is not None:
            return after
    return None


def _conclude(task, failed):
    if failed == LEASE_LOST:
        return {"state": LEASE_LOST}
    if failed in ("contract_rejected", "checker_mismatch"):
        return {"state": _finish(task, "blocked", error={"code": failed})}
    if failed == "source_unreadable":
        return {"state": _finish(task, "waiting", delay=3600)}
    if failed:
        return {"state": _finish(task, "waiting", delay=min(3600, 30 * 2 ** task["attempts"]), error={"code": failed})}
    return {"state": _finish(task, "done")}


def process_once(*, client=None):
    task = _claim()
    if task is None:
        return {"state": "idle"}
    run = {"id": uuid4().hex, "batches": 0, "index": -1}
    work, stop = _load(task, run)
    if stop is not None:
        return stop
    requests, unbuildable = _build(work)
    run["batches"] = len(requests)
    stop = _record_unsent(work, requests, unbuildable)
    if stop is not None:
        return stop
    owned = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
    try:
        failed = _send_batches(client, work, requests)
    finally:
        if owned:
            client.close()
    if failed is None and task["producer"] == "measures":
        failed = _publish(work)
    return _conclude(task, failed)


def for_versions(source_id, is_demo, version_ids, mode, cutoff):
    """The checks of visible versions, per producer: the latest run for the current checker counts as checked only
    when every batch of it completed; its items are all batches' items and unbuildable items (unchecked). The binding
    returned names the producer, producer version, clean-text processor and text_sha256, the checker and the run.
    Without such a run the producer is "unchecked" with the reason - never a pass, never an older checker's result."""
    import re

    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    eligible = history._eligibility(mode).replace("first_seen_at", "v.first_seen_at").replace(
        "proof_available_at", "v.proof_available_at")
    checker = expected_checker()
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        history._source(conn, source_id)
        cutoff = history._cutoff(cutoff)
        if cutoff > history._now(conn):
            raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        visible = [r[0] for r in conn.execute(text(
            "SELECT v.version_id FROM history_versions v WHERE v.source_id=:s AND v.is_demo=:d AND v.version_id=ANY(:ids) "
            "AND " + eligible), {"s": source_id, "d": is_demo, "ids": list(version_ids), "cutoff": cutoff})]
        rows = [dict(r) for r in conn.execute(text(
            "SELECT version_id,producer,producer_version,text_processor_version,text_sha256,checker_version,status,"
            "error_code,items,counts,run_id,run_batches,batch_index,created_at FROM derived_extraction_checks "
            "WHERE version_id=ANY(:ids) AND is_demo=:d ORDER BY id"), {"ids": visible, "d": is_demo}).mappings()]
        tasks = [dict(r) for r in conn.execute(text(
            "SELECT version_id,producer,state FROM derived_extraction_check_tasks WHERE version_id=ANY(:ids) "
            "AND checker_version=:c"), {"ids": visible, "c": checker}).mappings()]
    out = {}
    for v in visible:
        producers = {}
        for p in PRODUCERS:
            mine = [r for r in rows if r["version_id"] == v and r["producer"] == p]
            runs = {}
            for r in mine:
                runs.setdefault(r["run_id"], []).append(r)
            current = [rs for rs in runs.values() if any(r["checker_version"] == checker for r in rs)
                       or all(r["checker_version"] is None for r in rs)
                       or any(r["error_code"] == "checker_mismatch" for r in rs)]
            latest = max(current, key=lambda rs: max(r["created_at"] for r in rs)) if current else None
            batches = [r for r in latest or [] if r["batch_index"] >= 0]
            complete = bool(latest) and all(r["status"] == "completed" and r["checker_version"] == checker
                                            for r in batches) and len({r["batch_index"] for r in batches}) == (
                latest[0]["run_batches"] if latest else -1)
            items = [i for r in sorted(latest or [], key=lambda r: r["batch_index"]) for i in r["items"]]
            counts = {}
            for i in items:
                counts[i.get("status", "unchecked")] = counts.get(i.get("status", "unchecked"), 0) + 1
            state = "checked" if complete and latest[0]["run_batches"] > 0 else "unchecked"
            reason = None
            if state != "checked":
                bad = next((r for r in reversed(latest or []) if r["status"] != "completed"), None)
                pending = any(t["version_id"] == v and t["producer"] == p and t["state"] in ("queued", "waiting")
                              for t in tasks)
                reason = (bad["error_code"] if bad else "no_items" if latest and latest[0]["run_batches"] == 0
                          else "pending_for_current_checker" if pending or not latest else "incomplete_run")
            producers[p] = {"state": state, "reason": reason, "checker_version": checker if state == "checked" else None,
                            "binding": ({k: latest[0][k] for k in ("producer_version", "text_processor_version",
                                                                    "text_sha256", "run_id", "run_batches")} if latest else None),
                            "counts": counts if latest else None, "items": items if state == "checked" else []}
        out[v.strip()] = producers
    return {"source_id": source_id, "is_demo": is_demo, "contract_version": CONTRACT, "checker_version": checker,
            "versions": out}


def run_worker(*, stop, idle_seconds=5.0, backfill_every=60.0, rounds=None):
    last, n = 0.0, 0
    while not stop.is_set() and (rounds is None or n < rounds):
        n += 1
        if time.monotonic() - last >= backfill_every:
            last = time.monotonic()
            backfill()
            backfill_measures()
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
