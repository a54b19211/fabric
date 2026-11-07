"""Measurement extraction runs of the processing layer (datafabric.measure_extract): plan, read, settle, owe, retry.

- plan(): a run per current clean artifact of a fixed original whose source may be measured (processing.registry,
  record kind original only - a graph package, report or computed result never goes back into extraction) and is
  readable now; the run key is the original, the artifact and the producer, so planning again inserts nothing. A
  clean-text upgrade gives a new artifact and a new run that supersedes the old one; a producer upgrade (another
  model configuration or processor version) is planned only when asked (producer_upgrade=True: a budgeted backfill,
  never automatic). The old run keeps its identity and its windows as they are; a process claims windows only of
  the runs of its own producer.
  The versions a bounded backfill created (db.history.background_version_ids) are not planned by it: background
  material is measured only when a scope names it.
- plan_scope(): the one entry that plans an explicit list (Scope) of versions, each with all its reading windows or
  only the listed ones. The list's digest and the windows chosen are written on the run (selection); a window the
  list did not name is 'skipped' (out_of_scope), never owed. Its receipt says what was planned, what a run already
  held and why any version was left out. process_once(scope=...) reads nothing outside the list. With
  producer_upgrade=True only a run by the current producer counts as held: a listed version that older producers
  read gets a new run by this one (background versions included, nothing outside the list), and the old runs stay.
- two candidate routes, one queue (processing.measure_extract): a model route and a rules route (the explicit counts
  of who.explicit-count.v1, no model, no request). Both plan runs, read windows, store readings and queue their
  BioCheck check through the same tables; the producer identity names the route and a route supersedes only itself.
- process_once(): one window under a lease. The artifact is re-read (hash recomputed) and the source permission
  re-checked; then one model call through llm_transport, whose before_attempt re-checks the lease and the permission
  and registers the request in its own committed transaction before the HTTP request (revoked meanwhile: nothing is
  sent), and whose after_attempt settles it with the raw response. A spent budget (the round's, or a 429/503) leaves
  the window budget_spent - owed, not failed, attempts unchanged. A failure is retried with backoff and after
  MAX_ATTEMPTS is failed_final: the run is partial and the window owed until retry_failed() re-queues it. The result
  is written only while the lease is still this worker's and the permission still holds. A rules window is read the
  same way without the model call, and a fault of the rules is a failed window like any other.
- settle(): the run's state from its windows; a run that is complete or partial relocates what its superseded run
  read and (re)queues its BioCheck task (producer "measures", db.extraction_checks).
- coverage(): per version and route, what was read and what is owed - partial means a coverage gap, not "no numbers
  there" - and, where both routes read the same artifact, whether their readings agree (never as independent proof).
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
from processing import measure_agreement as agreement
from processing import measure_counts as counts
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


def rules_from_env() -> bool:
    """Whether this deployment also runs the rules route (no model, no request): on unless switched off."""
    return os.environ.get("DATAFABRIC_MEASURE_RULES_ROUTE", "true").lower() != "false"


@dataclass(frozen=True)
class Route:
    """A candidate route that plans runs: its name, the producer key that signs them and, for the model route, the
    model. The configuration stored with a run is the model's, or for the rules route the rule set."""

    name: str
    key: dict
    model: Model | None

    @property
    def config(self) -> dict:
        return self.model.config.__dict__ if self.model else {"route": mx.RULES, "method": counts.METHOD}


def routes_of(model: Model | None, with_rules: bool) -> list[Route]:
    routes = [Route(mx.MODEL, producer_key(model), model)] if model else []
    if with_rules:
        key = {"name": mx.PRODUCER, "version": mx.PROCESSOR_VERSION, "model_config_sha256": None,
               "identity": mx.rules_identity(counts.METHOD)}
        routes.append(Route(mx.RULES, key, None))
    return routes


def _is_rules(alias: str) -> str:
    """SQL: the producer identity of `alias` names the rules route."""
    return f"strpos({alias}.producer_identity, '{mx.RULES_MARK}') > 0"


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


# The runs that count as planned already for a route: any run of the route or, on a producer upgrade, only a run by
# this producer (its identity: processor version and model configuration digest).
_HELD_BY = (f"CASE WHEN CAST(:upgrade AS boolean) THEN r.producer_identity = :identity "
            f"ELSE ({_is_rules('r')}) = CAST(:rules AS boolean) END")

# The clean artifacts that have no run held by `route` (see _HELD_BY).
# {pick} is what the call selects by: the standing queue (all but the bounded backfill's versions) or a listed scope.
_CANDIDATES = """
    SELECT DISTINCT ON (c.version_id) c.version_id, c.processor_version, c.text_sha256, c.status, c.text,
           v.source_id, v.source_record_id, v.is_demo, v.record_kind, v.payload->>'raw_sha256' AS raw_sha256
      FROM derived_clean_text c JOIN history_versions v ON v.version_id = c.version_id
     WHERE c.processor_version = :tpv AND v.source_id = ANY(:sources) AND v.record_kind <> 'graph' AND {pick}
       AND NOT EXISTS (SELECT 1 FROM derived_measure_runs r WHERE r.version_id = c.version_id
                         AND r.text_processor_version = c.processor_version AND r.text_sha256 = c.text_sha256
                         AND {held_by})
     ORDER BY c.version_id, c.attempt DESC
     LIMIT :n
"""
_STANDING = "v.version_id <> ALL(CAST(:background AS text[]))"
_LISTED = "v.version_id = ANY(CAST(:ids AS text[]))"


def _candidates(conn, route, pick, params, *, upgrade=False, n=100):
    from processing import clean_text

    sql = _CANDIDATES.format(pick=pick, held_by=_HELD_BY)
    return conn.execute(text(sql), {**params, "tpv": clean_text.PROCESSOR_VERSION, "upgrade": upgrade,
                                    "identity": route.key["identity"], "rules": route.name == mx.RULES,
                                    "n": n}).mappings().all()


def _sources(conn, route, source_id=None):
    """The sources this route reads now: measurable ones and, for the rules route, those it has rules for."""
    from processing import registry

    wanted = [source_id] if source_id else registry.MEASURE_SOURCES
    return [s for s in wanted if (route.name == mx.MODEL or s in counts.SOURCES) and measurable(conn, s)]


def plan(batch=100, *, model: Model | None = None, producer_upgrade=False, source_id=None, with_rules=False) -> int:
    """Runs for current clean artifacts that have none by a route (producer_upgrade: none by this producer). The
    versions a bounded backfill created are background and left out: only plan_scope() takes them. Returns how many."""
    routes = routes_of(model or model_from_env(), with_rules)
    if not routes:
        return 0
    with history.history_engine().begin() as conn:
        params = {"background": history.background_version_ids(conn)}
        made = 0
        for route in routes:
            params["sources"] = _sources(conn, route, source_id)
            if params["sources"]:
                rows = _candidates(conn, route, _STANDING, params, upgrade=producer_upgrade, n=batch)
                made += sum(_plan_one(conn, dict(row), route) is not None for row in rows if row["raw_sha256"])
        return made


# ---- planning an explicit list ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """An explicit list of what to measure: version ids, each with all its reading windows (None) or only the
    listed window indexes. Nothing outside the list is planned or read through it."""

    versions: dict[str, tuple[int, ...] | None]

    @classmethod
    def parse(cls, data) -> Scope:
        """From {"versions": [<version id> | {"version_id": ..., "windows": [<index>, ...]}, ...]}."""
        entries = data.get("versions") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError('a scope is {"versions": [...]} with at least one version')
        versions = {}
        for entry in entries:
            version, windows = _entry(entry)
            if version in versions:
                raise ValueError(f"version listed twice: {version}")
            versions[version] = windows
        return cls(versions)

    @property
    def sha256(self) -> str:
        return mx.sha256({v: list(w) if w is not None else None for v, w in sorted(self.versions.items())})

    def selection(self, version_id) -> dict:
        """What is written on the run of a listed version."""
        windows = self.versions[version_id]
        return {"scope_sha256": self.sha256, "windows": list(windows) if windows is not None else None}


def _entry(entry):
    version, windows = (entry, None) if isinstance(entry, str) else (
        (entry.get("version_id"), entry.get("windows")) if isinstance(entry, dict) else (None, None))
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
        raise ValueError(f"not a version id: {entry!r}")
    if windows is not None:
        if (not isinstance(windows, list) or not windows or len(set(windows)) != len(windows)
                or any(not isinstance(i, int) or isinstance(i, bool) or i < 0 for i in windows)):
            raise ValueError(f"the windows of {version} must be distinct non-negative indexes")
        windows = tuple(sorted(windows))
    return version, windows


def plan_scope(scope: Scope, *, model: Model | None = None, with_rules=False, producer_upgrade=False) -> dict:
    """Runs for the versions of an explicit list only, by every route this call has. The receipt says per route what
    was planned (version -> run), which versions a run already held and why any other was left out: a listed version
    that cannot be planned (no clean artifact yet, a window that does not exist) is reported, never skipped silently.
    A run of the route on the same artifact is held (already_planned) and left as it is; with producer_upgrade only
    a run by the current producer is, so a version an older producer read gets a new run that supersedes the old one
    of its route, which keeps its identity."""
    ids = list(scope.versions)
    receipt = {"schema": "datafabric.measure_plan.v1", "scope_sha256": scope.sha256, "requested": len(ids),
               "routes": {}}
    with history.history_engine().begin() as conn:
        for route in routes_of(model or model_from_env(), with_rules):
            sources = _sources(conn, route)
            rows = _candidates(conn, route, _LISTED, {"sources": sources, "ids": ids}, upgrade=producer_upgrade,
                               n=len(ids)) if sources else []
            planned, left_out = {}, {}
            for row in rows:
                version = row["version_id"].strip()
                why = _refusal(row, scope.versions[version])
                if why is None and (run_id := _plan_one(conn, dict(row), route, scope.selection(version))):
                    planned[version] = run_id
                elif why is not None:
                    left_out[version] = why
            for version in ids:
                if version not in planned and version not in left_out:
                    left_out[version] = _why_not(conn, route, version, sources, producer_upgrade)
            receipt["routes"][route.name] = {
                "planned": planned, "already_planned": [v for v, why in left_out.items() if why == "already_planned"],
                "not_planned": {v: why for v, why in left_out.items() if why != "already_planned"}}
    return receipt


def _spans(row):
    return rules.windows_of(row["text"]) if row["status"] == "parsed" and row["text"] else []


def _refusal(row, windows):
    """Why a listed candidate is not planned (None when it is)."""
    if not row["raw_sha256"]:
        return "raw_sha256_unknown"
    if windows is not None and windows[-1] >= len(_spans(row)):
        return "window_out_of_range"
    return None


def _why_not(conn, route, version, sources, upgrade=False):
    """Why a listed version has no new run by this route: one it already has (by this producer, on an upgrade), or
    the first condition it fails."""
    from processing import clean_text

    known = conn.execute(text("SELECT source_id, record_kind FROM history_versions WHERE version_id=:v"),
                         {"v": version}).first()
    if known is None:
        return "version_unknown"
    held = conn.execute(text(
        f"SELECT 1 FROM derived_measure_runs r WHERE r.version_id=:v AND r.text_processor_version=:tpv "
        f"AND {_HELD_BY} LIMIT 1"),
        {"v": version, "tpv": clean_text.PROCESSOR_VERSION, "rules": route.name == mx.RULES, "upgrade": upgrade,
         "identity": route.key["identity"]}).first()
    if held:
        return "already_planned"
    if known[0] not in sources:
        return "source_not_read_by_this_route"
    return "record_kind_graph" if known[1] == "graph" else "no_clean_artifact_at_the_current_cleaner"


def _plan_one(conn, row, route, selection=None) -> str | None:
    """Insert the run of one candidate with its windows; its run id, or None when it already exists. A listed
    window selection leaves the other windows 'skipped' (out_of_scope)."""
    key = route.key
    material = {"source_id": row["source_id"], "record_id": row["source_record_id"], "version_id": row["version_id"],
                "raw_sha256": row["raw_sha256"]}
    artifact = {"text_processor_version": row["processor_version"], "text_sha256": row["text_sha256"]}
    run_id = mx.run_id(material, artifact, key)
    spans = _spans(row)
    previous = conn.execute(text(
        "SELECT run_id FROM derived_measure_runs WHERE version_id=:v AND run_id<>:r "
        f"AND ({_is_rules('derived_measure_runs')}) = CAST(:rules AS boolean) ORDER BY created_at DESC LIMIT 1"),
        {"v": row["version_id"], "r": run_id, "rules": route.name == mx.RULES}).scalar()
    made = conn.execute(text("""
        INSERT INTO derived_measure_runs(run_id,version_id,source_id,source_record_id,is_demo,raw_sha256,
          text_processor_version,text_sha256,producer,producer_version,model_config_sha256,model_config,
          producer_identity,window_chars,overlap_chars,windows_total,state,reason,supersedes_run_id,selection)
        VALUES (:r,:v,:s,:rec,:d,:raw,:tpv,:sha,:p,:pv,:cfg,CAST(:config AS jsonb),:pi,:wc,:oc,:n,:state,:reason,:prev,
                CAST(:selection AS jsonb))
        ON CONFLICT DO NOTHING"""), {
        "r": run_id, "v": row["version_id"], "s": row["source_id"], "rec": row["source_record_id"],
        "d": row["is_demo"], "raw": row["raw_sha256"], "tpv": row["processor_version"], "sha": row["text_sha256"],
        "p": key["name"], "pv": key["version"], "cfg": key["model_config_sha256"],
        "config": json.dumps(route.config), "pi": key["identity"], "wc": rules.WINDOW,
        "oc": rules.OVERLAP, "n": len(spans), "state": "reading" if spans else "failed",
        "reason": None if spans else "no_clean_artifact", "prev": previous,
        "selection": json.dumps(selection) if selection else None}).rowcount
    if made and spans:
        chosen = set(selection["windows"]) if selection and selection["windows"] is not None else None
        conn.execute(text("INSERT INTO derived_measure_windows(run_id,window_index,start_char,end_char,state,reason) "
                          "VALUES (:r,:i,:a,:b,:state,:why)"),
                     [{"r": run_id, "i": i, "a": a, "b": b, "state": "skipped" if chosen and i not in chosen
                       else "pending", "why": "out_of_scope" if chosen and i not in chosen else None}
                      for i, (a, b) in enumerate(spans)])
    return run_id if made else None


# ---- one window ---------------------------------------------------------------------------------------------


def _claim(routes: list[Route], scope=None):
    """The next due window of a run by the current producer of one of these routes (the producer identity names the
    processor version and the model configuration digest) and, when a scope is given, of its listed versions only
    and, where it names windows, only of those windows - also in a run the standing queue planned before. A run of
    another producer is left as it is: this process would read its windows with its own prompt and file the
    readings under the old identity."""
    whole = [v for v, windows in scope.versions.items() if windows is None] if scope else None
    named = [f"{v}:{i}" for v, windows in scope.versions.items() for i in windows or ()] if scope else None
    with history.history_engine().begin() as conn:
        return WINDOWS.claim(
            conn, lease_seconds=LEASE_SECONDS, alias="w",
            columns="w.*, r.version_id, r.source_id, r.source_record_id, r.is_demo, r.raw_sha256, "
                    "r.text_processor_version, r.text_sha256, r.producer_identity, r.model_config_sha256",
            joins="JOIN derived_measure_runs r ON r.run_id = w.run_id",
            where="r.state = 'reading' AND w.state IN ('pending','failed','budget_spent') "
                  "AND w.next_at <= clock_timestamp() AND r.producer_identity = ANY(CAST(:identities AS text[])) "
                  "AND (CAST(:whole AS text[]) IS NULL OR r.version_id = ANY(CAST(:whole AS text[])) "
                  "OR r.version_id || ':' || w.window_index = ANY(CAST(:named AS text[])))",
            params={"identities": [route.key["identity"] for route in routes], "whole": whole, "named": named},
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


@dataclass(frozen=True)
class _Read:
    """What a window reads: the artifact text with its segments, and the title of the original."""

    text: str
    segments: list
    title: str


def _artifact(conn, w):
    """The run's artifact (text, segments), or None when it is no longer there exactly (hash recomputed)."""
    row = conn.execute(text(
        "SELECT text,text_sha256,segments FROM derived_clean_text WHERE version_id=:v AND processor_version=:tpv "
        "AND text_sha256=:sha AND status='parsed' ORDER BY attempt DESC LIMIT 1"),
        {"v": w["version_id"], "tpv": w["text_processor_version"], "sha": w["text_sha256"]}).mappings().first()
    if row is None or mx.sha256(row["text"]) != w["text_sha256"]:
        return None
    return row["text"], row["segments"]


def _title(conn, w):
    return conn.execute(text("SELECT payload->>'title' FROM history_versions WHERE version_id=:v"),
                        {"v": w["version_id"]}).scalar() or ""


def _prepare(w, budget):
    """(what the window reads, None), or (None, final state) when the window ends before any read; a window of the
    model route ends here too when the round's request budget is spent."""
    with history.history_engine().begin() as conn:
        if not WINDOWS.holds(conn, w, lock=True):
            return None, "lease_lost"
        if not _readable(conn, w["source_id"]):
            _attempt(conn, w, "source_unreadable")
            return None, _set_window(conn, w, "source_unreadable", reason="source_unreadable")
        artifact = _artifact(conn, w)
        if artifact is None:
            _attempt(conn, w, "artifact_changed")
            _set_window(conn, w, "failed_final", reason="artifact_changed")
            settle(conn, w["run_id"])
            return None, "failed_final"
        if budget is not None and budget.spent():
            _attempt(conn, w, "budget_spent", error="request_budget")
            return None, _set_window(conn, w, "budget_spent", reason="request_budget")
        return _Read(*artifact, _title(conn, w)), None


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
    return _store(w, full, measures, why, common)


def _read_by_rules(w, read):
    """Read a window by the rules route: no model, no request. What the rules found is stored exactly as a model's
    answer is; a fault of the rules fails the window (retried, then owed) and never leaves its lease held."""
    try:
        measures, skipped = counts.measures_of(read.text, read.segments, (w["start_char"], w["end_char"]),
                                               w["source_record_id"])
    except Exception as exc:
        reason = f"rules_error:{type(exc).__name__}"
        with history.history_engine().begin() as conn:
            if not WINDOWS.holds(conn, w, lock=True):
                return "lease_lost"
            _attempt(conn, w, "failed", error=reason)
            state = _set_window(conn, w, "failed", failure=True, reason=reason)
            settle(conn, w["run_id"])
            return state
    output = mx.canonical({"items": [{"index": 1, "measures": measures}]})
    common = {"call_id": None, "requests": 0, "output": output, "output_sha256": mx.sha256(output)}
    return _store(w, read.text, measures, None, common, skipped)


def _store(w, full, measures, why, common, skipped=()):
    """Store what a route answered for the window: its readings, and what could not become one with the reason
    (only while the lease and the permission still hold; `why`: the answer was not usable)."""
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
                 not_built=[*not_built, *skipped], **common)
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
                 budget: RequestBudget | None = None, scope: Scope | None = None, with_rules=False) -> dict:
    """One due window of a run by the current producer of a route this process serves - the model route when a model
    is configured, the rules route when with_rules - and, with a scope, of the listed versions and windows only. The
    request budget is the model's alone."""
    model = model or model_from_env()
    if model is None and not with_rules:
        return {"state": "disabled"}
    w = _claim(routes_of(model, with_rules), scope)
    if w is None:
        return {"state": "idle"}
    by_rules = mx.route_of(w["producer_identity"]) == mx.RULES
    read, stop = _prepare(w, None if by_rules else budget)
    if stop is not None:
        return {"state": stop}
    if by_rules:
        return {"state": _read_by_rules(w, read)}
    owned = client is None
    client = client or httpx.Client(follow_redirects=False, trust_env=False)
    try:
        record, attempt, blocked, call_id = _call(model, w, read.text, read.title, client)
    finally:
        if owned:
            client.close()
    if budget is not None:
        budget.spend(record.requests)
    kind, _, content = _outcome(attempt) if blocked is None else (None, None, None)
    if kind == "ok":
        return {"state": _write(w, read.text, content, record, call_id)}
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
    """Per visible version, its latest run of each route: state, windows read, windows its list did not name and
    every owed window with its state and reason; the first-level fields are the model route's (the rules route's
    when only it ran). Where both routes read the same artifact: whether their readings agree (route_agreement)."""
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
            "SELECT DISTINCT ON (version_id, is_rules) * FROM (SELECT *, " + _is_rules("derived_measure_runs")
            + " AS is_rules FROM derived_measure_runs WHERE version_id=ANY(:ids) AND is_demo=:d) latest "
            "ORDER BY version_id, is_rules, created_at DESC"), {"ids": visible, "d": is_demo}).mappings().all()
        windows = conn.execute(text(
            "SELECT run_id, window_index, start_char, end_char, state, attempts, reason FROM derived_measure_windows "
            "WHERE run_id=ANY(:r) ORDER BY run_id, window_index"), {"r": [r["run_id"] for r in runs]}).mappings().all()
        from db.measurement_reads import Readable, unresolved_at

        claims = unresolved_at(conn, runs, mode, cutoff, Readable(conn))
        agreed = _agreements(conn, runs)
    out = {v.strip(): {"state": "not_planned"} for v in visible}
    for version, mine in _by_version(runs).items():
        routes = {mx.route_of(run["producer_identity"]): _summary(run, windows) for run in mine}
        key = (mine[0]["source_id"], mine[0]["source_record_id"], mine[0]["is_demo"])
        out[version] = {**(routes.get(mx.MODEL) or routes[mx.RULES]), "routes": routes,
                        "route_agreement": agreed.get(version), "unresolved_corrections": claims.get(key, [])}
    return {"schema": "datafabric.measure_coverage.v1", "source_id": source_id, "is_demo": is_demo, "mode": mode,
            "note": "partial: the measurements of the windows read stand; an owed window is a coverage gap, "
                    "not a sign that it holds no number",
            "versions": out}


def _by_version(runs):
    out = {}
    for run in runs:
        out.setdefault(run["version_id"].strip(), []).append(run)
    return out


def _summary(run, windows):
    """One run in the coverage receipt: its route, state, windows read, windows its list did not name (out_of_scope,
    not owed) and the owed ones; `selection` is how the run was chosen (None: by the standing queue)."""
    mine = [w for w in windows if w["run_id"] == run["run_id"]]
    return {"run_id": run["run_id"], "route": mx.route_of(run["producer_identity"]), "state": run["state"],
            "reason": run["reason"], "windows_total": run["windows_total"],
            "read": sum(w["state"] == "sent" for w in mine), "out_of_scope": sum(w["state"] == "skipped" for w in mine),
            "selection": run["selection"],
            "owed": [{"window_index": w["window_index"], "start": w["start_char"], "end": w["end_char"],
                      "state": w["state"], "attempts": w["attempts"], "reason": w["reason"]}
                     for w in mine if w["state"] in OWED],
            "producer_version": mx.PROCESSOR_VERSION, "producer_identity": run["producer_identity"],
            "text_processor_version": run["text_processor_version"], "text_sha256": run["text_sha256"]}


def _agreements(conn, runs):
    """version -> whether its two routes agree (processing.measure_agreement), for the versions where both routes
    read the same artifact."""
    pairs = {}
    for version, mine in _by_version(runs).items():
        by_route = {mx.route_of(run["producer_identity"]): run for run in mine}
        a, b = by_route.get(mx.RULES), by_route.get(mx.MODEL)
        if a and b and (a["text_processor_version"], a["text_sha256"]) == (b["text_processor_version"], b["text_sha256"]):
            pairs[version] = (a, b)
    if not pairs:
        return {}
    readings = {}
    ids = [run["run_id"] for pair in pairs.values() for run in pair]
    for row in conn.execute(text("SELECT run_id, reading_id, item, final FROM derived_measure_readings "
                                 "WHERE run_id = ANY(:r)"), {"r": ids}).mappings():
        readings.setdefault(row["run_id"], []).append(dict(row))
    return {version: agreement.agreement(readings.get(a["run_id"], []), readings.get(b["run_id"], []))
            for version, (a, b) in pairs.items()}
