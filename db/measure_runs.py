"""Measurement extraction runs of the processing layer (datafabric.measure_extract): plan, read, settle, owe, retry.

- plan(): a run per current clean artifact of a fixed original whose source may be measured (processing.registry,
  record kind original only - a graph package, report or computed result never goes back into extraction) and is
  readable now; the run key is the original, the artifact and the producer, so planning again inserts nothing. A
  clean-text upgrade gives a new artifact and a new run that supersedes the old one; a producer upgrade (another
  model configuration) is planned only when asked (producer_upgrade=True: a budgeted backfill, never automatic).
- process_once(): one window under a lease. The artifact is re-read (hash recomputed) and the source permission
  re-checked; then one model call through llm_transport, whose before_attempt re-checks the lease and the permission
  and registers the request in its own committed transaction before the HTTP request (revoked meanwhile: nothing is
  sent), and whose after_attempt settles it with the raw response. A spent budget (the round's, or a 429/503) leaves
  the window budget_spent - owed, not failed, attempts unchanged. A failure is retried with backoff and after
  MAX_ATTEMPTS is failed_final: the run is partial and the window owed until retry_failed() re-queues it. The result
  is written only while the lease is still this worker's and the permission still holds.
- settle(): the run's state from its windows; a run that is complete or partial relocates what its superseded run
  read and (re)queues its BioCheck task (producer "measures", db.extraction_checks).
- coverage(): per version, what was read and what is owed - partial means a coverage gap, not "no numbers there".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from uuid import uuid4

import httpx
import llm_transport as lt
from sqlalchemy import text

from biz_rules.history_models import HistoryError
from db import history
from db.lease import LeaseQueue, backoff
from processing import measure_extract as mx
from processing import measure_rules as rules

LEASE_SECONDS = 180
MAX_ATTEMPTS = 3
RETRY_BASE, RETRY_CAP = 300, 3600
BUDGET_STATUSES = (429, 503)  # the provider says "not now": a budget, never a failure of the window
TO_READ = ("pending", "failed", "budget_spent")
OWED = ("pending", "failed", "failed_final", "budget_spent", "source_unreadable")
WINDOWS = LeaseQueue("derived_measure_windows", ("run_id", "window_index"))


# ---- configuration -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Model:
    provider: lt.Provider
    config: mx.ModelConfig


def model_from_env() -> Model | None:
    """The model of this deployment, or None when measurement extraction is not configured (nothing is planned)."""
    base = os.environ.get("DATAFABRIC_MEASURE_LLM_BASE_URL", "")
    name = os.environ.get("DATAFABRIC_MEASURE_LLM_MODEL", "")
    if not base or not name:
        return None
    kind = os.environ.get("DATAFABRIC_MEASURE_LLM_KIND", "openai_compatible")
    endpoint = os.environ.get("DATAFABRIC_MEASURE_LLM_ENDPOINT_ID", "default")
    thinking = os.environ.get("DATAFABRIC_MEASURE_LLM_THINKING", "false").lower() == "true"
    max_tokens = int(os.environ.get("DATAFABRIC_MEASURE_LLM_MAX_TOKENS", "2000"))
    provider = lt.Provider(kind=kind, base_url=base, model=name, api_key=os.environ.get("DATAFABRIC_MEASURE_LLM_KEY", ""),
                           timeout_seconds=float(os.environ.get("DATAFABRIC_MEASURE_LLM_TIMEOUT", "60")),
                           endpoint_id=endpoint)
    config = mx.ModelConfig(kind=kind, model=name, endpoint_id=endpoint, thinking=thinking,
                            temperature=None if thinking else 0.0, max_tokens=max_tokens)
    return Model(provider, config)


def producer_key(model: Model) -> dict:
    sha = model.config.sha256()
    return {"name": mx.PRODUCER, "version": mx.PROCESSOR_VERSION, "model_config_sha256": sha,
            "identity": mx.producer_identity(sha)}


class RequestBudget:
    """The model requests one round may send; when it is spent, windows wait as budget_spent."""

    def __init__(self, requests: int | None):
        self.left = requests

    def spent(self) -> bool:
        return self.left is not None and self.left <= 0

    def spend(self, n: int):
        if self.left is not None:
            self.left -= n


# ---- planning ------------------------------------------------------------------------------------------------


def measurable(conn, source_id) -> bool:
    """Whether a source's versions may be read for measurements now: the processor applies to it, its records are
    originals (never a graph package or another derived product) and it is readable."""
    from biz_rules.source_capabilities import record_kind
    from processing import registry

    if "measure_extract" not in registry.applicable(source_id) or record_kind(source_id) != "original":
        return False
    return _readable(conn, source_id)


_CANDIDATES = """
    SELECT DISTINCT ON (c.version_id) c.version_id, c.processor_version, c.text_sha256, c.status, c.text,
           v.source_id, v.source_record_id, v.is_demo, v.record_kind, v.payload->>'raw_sha256' AS raw_sha256
      FROM derived_clean_text c JOIN history_versions v ON v.version_id = c.version_id
     WHERE c.processor_version = :tpv AND v.source_id = ANY(:sources) AND v.record_kind <> 'graph'
       AND NOT EXISTS (SELECT 1 FROM derived_measure_runs r WHERE r.version_id = c.version_id
                         AND r.text_processor_version = c.processor_version AND r.text_sha256 = c.text_sha256
                         AND (NOT CAST(:upgrade AS boolean) OR r.producer_identity = :identity))
     ORDER BY c.version_id, c.attempt DESC
     LIMIT :n
"""


def plan(batch=100, *, model: Model | None = None, producer_upgrade=False, source_id=None) -> int:
    """Runs for current clean artifacts that have none (producer_upgrade: none by this producer). Returns how many."""
    from processing import clean_text, registry

    model = model or model_from_env()
    if model is None:
        return 0
    key = producer_key(model)
    with history.history_engine().begin() as conn:
        sources = [s for s in ([source_id] if source_id else registry.MEASURE_SOURCES) if measurable(conn, s)]
        if not sources:
            return 0
        rows = conn.execute(text(_CANDIDATES), {"tpv": clean_text.PROCESSOR_VERSION, "sources": sources,
                                                 "upgrade": producer_upgrade, "identity": key["identity"],
                                                 "n": batch}).mappings().all()
        return sum(_plan_one(conn, dict(row), key, model) for row in rows if row["raw_sha256"])


def _plan_one(conn, row, key, model) -> int:
    material = {"source_id": row["source_id"], "record_id": row["source_record_id"], "version_id": row["version_id"],
                "raw_sha256": row["raw_sha256"]}
    artifact = {"text_processor_version": row["processor_version"], "text_sha256": row["text_sha256"]}
    run_id = mx.run_id(material, artifact, key)
    spans = rules.windows_of(row["text"]) if row["status"] == "parsed" and row["text"] else []
    previous = conn.execute(text(
        "SELECT run_id FROM derived_measure_runs WHERE version_id=:v AND run_id<>:r ORDER BY created_at DESC LIMIT 1"),
        {"v": row["version_id"], "r": run_id}).scalar()
    made = conn.execute(text("""
        INSERT INTO derived_measure_runs(run_id,version_id,source_id,source_record_id,is_demo,raw_sha256,
          text_processor_version,text_sha256,producer,producer_version,model_config_sha256,model_config,
          producer_identity,window_chars,overlap_chars,windows_total,state,reason,supersedes_run_id)
        VALUES (:r,:v,:s,:rec,:d,:raw,:tpv,:sha,:p,:pv,:cfg,CAST(:config AS jsonb),:pi,:wc,:oc,:n,:state,:reason,:prev)
        ON CONFLICT DO NOTHING"""), {
        "r": run_id, "v": row["version_id"], "s": row["source_id"], "rec": row["source_record_id"],
        "d": row["is_demo"], "raw": row["raw_sha256"], "tpv": row["processor_version"], "sha": row["text_sha256"],
        "p": key["name"], "pv": key["version"], "cfg": key["model_config_sha256"],
        "config": json.dumps(model.config.__dict__), "pi": key["identity"], "wc": rules.WINDOW,
        "oc": rules.OVERLAP, "n": len(spans), "state": "reading" if spans else "failed",
        "reason": None if spans else "no_clean_artifact", "prev": previous}).rowcount
    if made and spans:
        conn.execute(text("INSERT INTO derived_measure_windows(run_id,window_index,start_char,end_char,state) "
                          "VALUES (:r,:i,:a,:b,'pending')"),
                     [{"r": run_id, "i": i, "a": a, "b": b} for i, (a, b) in enumerate(spans)])
    return made


# ---- one window ---------------------------------------------------------------------------------------------


def _claim():
    with history.history_engine().begin() as conn:
        return WINDOWS.claim(
            conn, lease_seconds=LEASE_SECONDS, alias="w",
            columns="w.*, r.version_id, r.source_id, r.source_record_id, r.is_demo, r.raw_sha256, "
                    "r.text_processor_version, r.text_sha256, r.producer_identity, r.model_config_sha256",
            joins="JOIN derived_measure_runs r ON r.run_id = w.run_id",
            where="r.state = 'reading' AND w.state IN ('pending','failed','budget_spent') "
                  "AND w.next_at <= clock_timestamp()",
            order_by="w.next_at, w.run_id, w.window_index", also_set="updated_at=clock_timestamp()")


def _readable(conn, source_id) -> bool:
    try:
        history._source(conn, source_id)
        return True
    except HistoryError as exc:
        if exc.status != 403:
            raise
        return False


def _attempt(conn, w, outcome, **fields):
    conn.execute(text("""
        INSERT INTO derived_measure_window_attempts(run_id,window_index,outcome,error,call_id,requests,output_sha256,
          output,mappings,measures_found,reading_ids,not_built)
        VALUES (:r,:i,:o,:e,:call,:n,:osha,:out,CAST(:map AS jsonb),:found,CAST(:ids AS jsonb),CAST(:nb AS jsonb))"""),
        {"r": w["run_id"], "i": w["window_index"], "o": outcome, "e": fields.get("error"),
         "call": fields.get("call_id"), "n": fields.get("requests", 0), "osha": fields.get("output_sha256"),
         "out": fields.get("output"), "map": json.dumps(fields.get("mappings", [])), "found": fields.get("found"),
         "ids": json.dumps(fields.get("reading_ids", [])), "nb": json.dumps(fields.get("not_built", []),
                                                                          ensure_ascii=False)})


def _set_window(conn, w, state, *, failure=False, reason=None, delay=0.0):
    """Release the window in its new state; a failure counts an attempt and, after MAX_ATTEMPTS, is final."""
    attempts = w["attempts"] + (1 if failure else 0)
    if failure and attempts >= MAX_ATTEMPTS:
        state = "failed_final"
    if failure and state != "failed_final":
        delay = backoff(w["attempts"], base=RETRY_BASE, cap=RETRY_CAP)
    WINDOWS.release(conn, w, "state=:s,attempts=:a,reason=:why,next_at=clock_timestamp()+make_interval(secs=>:d),"
                             "updated_at=clock_timestamp()",
                    {"s": state, "a": attempts, "why": reason, "d": delay}, unexpired=False)
    if state == "source_unreadable":
        conn.execute(text("UPDATE derived_measure_runs SET state='paused_unreadable',reason='source_unreadable',"
                          "updated_at=clock_timestamp() WHERE run_id=:r AND state='reading'"), {"r": w["run_id"]})
    return state


def _artifact(conn, w):
    """The run's artifact text, or None when it is no longer there exactly (hash recomputed)."""
    row = conn.execute(text(
        "SELECT text,text_sha256 FROM derived_clean_text WHERE version_id=:v AND processor_version=:tpv "
        "AND text_sha256=:sha AND status='parsed' ORDER BY attempt DESC LIMIT 1"),
        {"v": w["version_id"], "tpv": w["text_processor_version"], "sha": w["text_sha256"]}).mappings().first()
    if row is None or mx.sha256(row["text"]) != w["text_sha256"]:
        return None
    return row["text"]


def _title(conn, w):
    return conn.execute(text("SELECT payload->>'title' FROM history_versions WHERE version_id=:v"),
                        {"v": w["version_id"]}).scalar() or ""


def _prepare(w, budget):
    """(artifact text, title, None), or (None, None, final state) when the window ends before any model call."""
    with history.history_engine().begin() as conn:
        if not WINDOWS.holds(conn, w, lock=True):
            return None, None, "lease_lost"
        if not _readable(conn, w["source_id"]):
            _attempt(conn, w, "source_unreadable")
            return None, None, _set_window(conn, w, "source_unreadable", reason="source_unreadable")
        full = _artifact(conn, w)
        if full is None:
            _attempt(conn, w, "artifact_changed")
            _set_window(conn, w, "failed_final", reason="artifact_changed")
            settle(conn, w["run_id"])
            return None, None, "failed_final"
        if budget is not None and budget.spent():
            _attempt(conn, w, "budget_spent", error="request_budget")
            return None, None, _set_window(conn, w, "budget_spent", reason="request_budget")
        return full, _title(conn, w), None


@dataclass
class _Call:
    """The durable audit of one model call: each HTTP request registered before it is sent, settled after."""

    w: dict
    call_id: str

    def before(self, start: lt.AttemptStart):
        with history.history_engine().begin() as conn:
            if not WINDOWS.holds(conn, self.w, lock=True):
                raise lt.SendBlocked("lease_lost")
            if not _readable(conn, self.w["source_id"]):
                raise lt.SendBlocked("source_unreadable")  # revoked between the read and the send: nothing goes out
            conn.execute(text(
                "INSERT INTO derived_measure_requests(call_id,request_index,run_id,window_index,body_sha256,"
                "endpoint_id,model) VALUES (:c,:i,:r,:w,:b,:e,:m)"),
                {"c": self.call_id, "i": start.index, "r": self.w["run_id"], "w": self.w["window_index"],
                 "b": start.body_sha256, "e": start.endpoint_id, "m": start.model})

    def after(self, attempt: lt.Attempt):
        with history.history_engine().begin() as conn:
            conn.execute(text(
                "UPDATE derived_measure_requests SET delivered=:d,http_status=:s,error=:e,exception=:x,timed_out=:t,"
                "response_sha256=:sha,response_text=:body,seconds=:sec,settled_at=clock_timestamp() "
                "WHERE call_id=:c AND request_index=:i"),
                {"d": attempt.delivered, "s": attempt.status, "e": attempt.error, "x": attempt.exception,
                 "t": attempt.timed_out, "sha": attempt.response_sha256, "body": attempt.response_text,
                 "sec": attempt.seconds, "c": self.call_id, "i": attempt.index})


def _call(model, w, full, title, client):
    """(record, attempt or None, blocked reason or None, call id): one model call over the window, every HTTP
    request of it registered before it is sent and settled after (derived_measure_requests)."""
    contexts = rules.contexts_of(full, w["start_char"])
    payload = mx.window_payload(title, full, (w["start_char"], w["end_char"]), contexts)
    body = lt.build_body(model.provider, mx.messages(payload), max_tokens=model.config.max_tokens,
                         thinking=model.config.thinking)
    record = lt.CallRecord(stage="measure_extract", endpoint_id=model.provider.endpoint_id, model=model.provider.model)
    audit = _Call(w, uuid4().hex)
    try:
        attempt = lt.send(model.provider, body, record, client=client, before_attempt=audit.before,
                          after_attempt=audit.after, max_requests=1)
    except lt.SendBlocked as exc:
        return record, None, exc.reason, audit.call_id
    return record, attempt, None, audit.call_id


def _outcome(attempt):
    """What the model's answer means for the window: ("budget"|"failed"|"ok", error, content)."""
    if attempt.status in BUDGET_STATUSES:
        return "budget", f"http_{attempt.status}", None
    if attempt.delivered != "yes" or attempt.status != 200:
        return "failed", attempt.error or "no_answer", None
    env = attempt.envelope()
    if env.problem:
        return "failed", env.problem, None
    return "ok", None, env.content


def _subject(w):
    return {"source_id": w["source_id"], "record_id": w["source_record_id"], "version_id": w["version_id"].strip(),
            "raw_sha256": w["raw_sha256"], "is_demo": bool(w["is_demo"]),
            "artifact": {"processor_version": w["text_processor_version"], "text_sha256": w["text_sha256"]}}


def keys_of(w, model_config_sha256):
    identity = w["producer_identity"]
    return {"material": {"source_id": w["source_id"], "record_id": w["source_record_id"],
                         "version_id": w["version_id"].strip(), "raw_sha256": w["raw_sha256"]},
            "artifact": {"text_processor_version": w["text_processor_version"], "text_sha256": w["text_sha256"]},
            "producer": {"name": mx.PRODUCER, "version": mx.PROCESSOR_VERSION,
                         "model_config_sha256": model_config_sha256, "identity": identity}}


def insert_readings(conn, w, drafts, *, relocated_from=None):
    for d in drafts:
        item = {k: v for k, v in d.body["items"][0].items() if k != "id"}
        conn.execute(text("""
            INSERT INTO derived_measure_readings(reading_id,run_id,window_index,version_id,source_id,is_demo,
              text_processor_version,text_sha256,producer_identity,quote_start,quote_end,item,passages,final,measure,
              relocated_from)
            VALUES (:id,:r,:wi,:v,:s,:d,:tpv,:sha,:pi,:q0,:q1,CAST(:item AS jsonb),CAST(:p AS jsonb),
                    CAST(:final AS jsonb),CAST(:m AS jsonb),:from) ON CONFLICT DO NOTHING"""),
            {"id": d.reading_id, "r": w["run_id"], "wi": w["window_index"], "v": w["version_id"], "s": w["source_id"],
             "d": w["is_demo"], "tpv": w["text_processor_version"], "sha": w["text_sha256"],
             "pi": w["producer_identity"], "q0": d.quote_start, "q1": d.quote_end,
             "item": json.dumps(item, ensure_ascii=False), "p": json.dumps(d.body["passages"], ensure_ascii=False),
             "final": json.dumps(d.final, ensure_ascii=False), "m": json.dumps(d.measure, ensure_ascii=False),
             "from": relocated_from})


def _write(w, full, content, record, call_id):
    """Store what the model answered: the readings (only while the lease and the permission still hold)."""
    measures, mappings, why = mx.parse_output(content)
    common = {"call_id": call_id, "requests": record.requests, "output": content, "mappings": mappings,
              "output_sha256": mx.sha256(content) if isinstance(content, str) else None}
    with history.history_engine().begin() as conn:
        if not WINDOWS.holds(conn, w, lock=True):
            return "lease_lost"  # a late answer: kept in derived_measure_requests, published by nobody
        if not _readable(conn, w["source_id"]):
            _attempt(conn, w, "source_unreadable", error="revoked_during_call", **common)
            return _set_window(conn, w, "source_unreadable", reason="source_unreadable")
        if why:
            _attempt(conn, w, "invalid_output", error=why, **common)
            state = _set_window(conn, w, "failed", failure=True, reason=why)
            settle(conn, w["run_id"])
            return state
        sent = rules.Sent((w["start_char"], w["end_char"]),
                          {k: v for k, v in rules.contexts_of(full, w["start_char"]).items() if v})
        drafts, not_built = mx.readings_of(measures, full, sent, _subject(w), keys_of(w, w["model_config_sha256"]))
        insert_readings(conn, w, drafts)
        _attempt(conn, w, "sent", found=len(measures), reading_ids=[d.reading_id for d in drafts],
                 not_built=not_built, **common)
        state = _set_window(conn, w, "sent")
        settle(conn, w["run_id"])
        return state


def _after_call(w, record, attempt, blocked, call_id):
    with history.history_engine().begin() as conn:
        if blocked == "lease_lost" or not WINDOWS.holds(conn, w, lock=True):
            return "lease_lost"
        if blocked:
            _attempt(conn, w, blocked, call_id=call_id)
            return _set_window(conn, w, "source_unreadable", reason=blocked)
        kind, error, _ = _outcome(attempt)
        if kind == "budget":
            _attempt(conn, w, "budget_spent", error=error, call_id=call_id, requests=record.requests)
            return _set_window(conn, w, "budget_spent", reason=error, delay=RETRY_BASE)
        _attempt(conn, w, "failed", error=error, call_id=call_id, requests=record.requests)
        state = _set_window(conn, w, "failed", failure=True, reason=error)
        settle(conn, w["run_id"])
        return state


def process_once(*, model: Model | None = None, client: httpx.Client | None = None,
                 budget: RequestBudget | None = None) -> dict:
    model = model or model_from_env()
    if model is None:
        return {"state": "disabled"}
    w = _claim()
    if w is None:
        return {"state": "idle"}
    full, title, stop = _prepare(w, budget)
    if stop is not None:
        return {"state": stop}
    owned = client is None
    client = client or httpx.Client(follow_redirects=False, trust_env=False)
    try:
        record, attempt, blocked, call_id = _call(model, w, full, title, client)
    finally:
        if owned:
            client.close()
    if budget is not None:
        budget.spend(record.requests)
    kind, _, content = _outcome(attempt) if blocked is None else (None, None, None)
    if kind == "ok":
        return {"state": _write(w, full, content, record, call_id)}
    return {"state": _after_call(w, record, attempt, blocked, call_id)}


# ---- the run ------------------------------------------------------------------------------------------------


def settle(conn, run_id):
    """The run's state from its windows (paused and failed runs keep theirs). A complete or partial run relocates
    what its superseded run read and (re)queues its check."""
    run = conn.execute(text("SELECT * FROM derived_measure_runs WHERE run_id=:r FOR UPDATE"),
                       {"r": run_id}).mappings().first()
    if run is None or run["state"] in ("paused_unreadable", "failed"):
        return run["state"] if run else None
    states = {r[0] for r in conn.execute(text("SELECT DISTINCT state FROM derived_measure_windows WHERE run_id=:r"),
                                         {"r": run_id})}
    if states & set(TO_READ):
        state, reason = "reading", None
    elif "failed_final" in states:
        state, reason = "partial", "windows_owed"
    else:
        state, reason = "complete", None
    conn.execute(text("UPDATE derived_measure_runs SET state=:s,reason=:why,updated_at=clock_timestamp() "
                      "WHERE run_id=:r"), {"s": state, "why": reason, "r": run_id})
    if state in ("complete", "partial"):
        from db import extraction_checks, measurements

        measurements.relocate(conn, dict(run))
        extraction_checks.enqueue_measures(conn, run["version_id"], run["producer_identity"],
                                           run["text_processor_version"])
    return state


def retry_failed(*, source_id=None, run_id=None) -> int:
    """Re-queue the owed windows (failed_final) - explicitly, with their attempts reset. Returns how many."""
    with history.history_engine().begin() as conn:
        n = conn.execute(text("""
            UPDATE derived_measure_windows w SET state='pending',attempts=0,reason=NULL,next_at=clock_timestamp(),
                   updated_at=clock_timestamp()
              FROM derived_measure_runs r
             WHERE r.run_id=w.run_id AND w.state='failed_final' AND r.state IN ('partial','reading')
               AND (CAST(:s AS text) IS NULL OR r.source_id=:s) AND (CAST(:r AS text) IS NULL OR r.run_id=:r)"""),
            {"s": source_id, "r": run_id}).rowcount
        conn.execute(text("""
            UPDATE derived_measure_runs r SET state='reading',reason=NULL,updated_at=clock_timestamp()
             WHERE r.state='partial' AND EXISTS (SELECT 1 FROM derived_measure_windows w WHERE w.run_id=r.run_id
                                                   AND w.state='pending')"""))
        return n


def resume(limit=50) -> int:
    """Paused runs whose source is readable again read on: their unreadable windows are owed again."""
    with history.history_engine().begin() as conn:
        rows = conn.execute(text("SELECT run_id, source_id FROM derived_measure_runs WHERE state='paused_unreadable' "
                                 "ORDER BY updated_at LIMIT :n FOR UPDATE SKIP LOCKED"), {"n": limit}).all()
        n = 0
        for run_id, source_id in rows:
            if not _readable(conn, source_id):
                continue
            conn.execute(text("UPDATE derived_measure_windows SET state='pending',next_at=clock_timestamp(),"
                              "updated_at=clock_timestamp() WHERE run_id=:r AND state='source_unreadable'"),
                         {"r": run_id})
            conn.execute(text("UPDATE derived_measure_runs SET state='reading',reason=NULL,updated_at=clock_timestamp() "
                              "WHERE run_id=:r"), {"r": run_id})
            n += 1
        return n


# ---- coverage ----------------------------------------------------------------------------------------------------


def coverage(source_id, is_demo, version_ids, mode, cutoff):
    """Per visible version, its latest run: state, windows read and every owed window with its state and reason."""
    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        history._source(conn, source_id)
        cutoff = history._cutoff(cutoff)
        if cutoff > history._now(conn):
            raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        visible = [r[0] for r in conn.execute(text(
            "SELECT version_id FROM history_versions WHERE source_id=:s AND is_demo=:d AND version_id=ANY(:ids) AND "
            + history._eligibility(mode)), {"s": source_id, "d": is_demo, "ids": list(version_ids), "cutoff": cutoff})]
        runs = conn.execute(text(
            "SELECT DISTINCT ON (version_id) * FROM derived_measure_runs WHERE version_id=ANY(:ids) AND is_demo=:d "
            "ORDER BY version_id, created_at DESC"), {"ids": visible, "d": is_demo}).mappings().all()
        windows = conn.execute(text(
            "SELECT run_id, window_index, start_char, end_char, state, attempts, reason FROM derived_measure_windows "
            "WHERE run_id=ANY(:r) ORDER BY run_id, window_index"), {"r": [r["run_id"] for r in runs]}).mappings().all()
        from db.measurement_reads import Readable, unresolved_at

        claims = unresolved_at(conn, runs, mode, cutoff, Readable(conn))
    out = {v.strip(): {"state": "not_planned"} for v in visible}
    for run in runs:
        mine = [w for w in windows if w["run_id"] == run["run_id"]]
        out[run["version_id"].strip()] = {
            "run_id": run["run_id"], "state": run["state"], "reason": run["reason"],
            "windows_total": run["windows_total"], "read": sum(w["state"] == "sent" for w in mine),
            "owed": [{"window_index": w["window_index"], "start": w["start_char"], "end": w["end_char"],
                      "state": w["state"], "attempts": w["attempts"], "reason": w["reason"]}
                     for w in mine if w["state"] in OWED],
            "producer_version": mx.PROCESSOR_VERSION, "producer_identity": run["producer_identity"],
            "text_processor_version": run["text_processor_version"], "text_sha256": run["text_sha256"],
            "unresolved_corrections": claims.get((run["source_id"], run["source_record_id"], run["is_demo"]), [])}
    return {"schema": "datafabric.measure_coverage.v1", "source_id": source_id, "is_demo": is_demo, "mode": mode,
            "note": "partial: the measurements of the windows read stand; an owed window is a coverage gap, "
                    "not a sign that it holds no number",
            "versions": out}
