"""Reading measurement objects: permission first, then visibility for the history mode, then the state at
the cutoff folded from the object's events. Every read checks the current permission of the object's source and of
each event's cause source (a correction from a revoked source is withheld, not applied); nothing is cached.
An object says on its own what its check found (check.status: consistent, inconsistent, unchecked); that is not its
state, which is folded from the events (ok, corrected, retracted, superseded, unknown).

Modes:
- current: every event; an object counts as current when its checker is the one expected now for measurements and
  it is not superseded (an object of an older checker is history until the current one re-checked its reading). An
  object no check has answered yet (checker "unchecked") has no checker to be out of date: it is current until
  the object of its check's answer supersedes it.
- operational_replay: what the system actually showed at the cutoff - objects produced by then, events produced by
  then whose material was visible by then; no later processor or checker result. Which checker was in force at the
  cutoff is not recorded (an append-only activation history is a follow-up), so an object
  of a checker other than the one configured now is not current in replay (checker_history_unknown) - never ok.
- public_history_reconstruction: today's processors and checker over material proven public by the cutoff - the
  version's proof, and corrections only from their own proof; the receipt says reconstruction: true and names the
  producer and checker versions used.
The change feed lists events in published_seq order (the commit order) for one demo kind.
"""

from __future__ import annotations

import re

from sqlalchemy import text

from clean import measurement_identity as identity
from clean.measurements import CORRECTING, NOT_CHECKED, SUPERSEDING
from store import data_objects as do
from store import history
from store.history_models import HistoryError

SCHEMA = "datafabric.measurements.v1"
CHANGES_SCHEMA = "datafabric.measurement_changes.v1"
MAX_OBJECTS, MAX_CHANGES = 2000, 200
RECONSTRUCTION = "public_history_reconstruction"
UNKNOWN_TARGET = "correction_target_version_unknown"


def expected_checker():
    from clean import extraction_checks

    return extraction_checks.expected_checker("measures")


class Readable:
    """The current permission of sources, asked once per source within one read (never kept across reads)."""

    def __init__(self, conn):
        self.conn, self.seen = conn, {}

    def __call__(self, source_id):
        if source_id not in self.seen:
            try:
                history._source(self.conn, source_id)
                self.seen[source_id] = True
            except HistoryError as exc:
                if exc.status != 403:
                    raise
                self.seen[source_id] = False
        return self.seen[source_id]


def _event_visible(e, mode, cutoff):
    if mode == "current":
        return True
    if mode == "operational_replay":
        return e["produced_at"] <= cutoff and e["material_visible_at"] <= cutoff
    if e["kind"] in CORRECTING:  # reconstruction: a correction counts once its own material is proven public
        return e["material_proof_at"] is not None and e["material_proof_at"] <= cutoff
    return True  # reconstruction uses today's processors and checker


def _summary(e):
    return {"kind": e["kind"], "seq": e["published_seq"], "produced_at": e["produced_at"].isoformat(),
            "effective_at": e["effective_at"].isoformat(), "material_visible_at": e["material_visible_at"].isoformat(),
            **({"superseded_by": e["superseded_by"].strip()} if e["superseded_by"] else {}),
            **({"cause": e["cause"]} if e["kind"] in CORRECTING else {})}


def fold(events, mode, cutoff, readable):
    """The state of one object at the cutoff from its events (in event order)."""
    applied, after, withheld, active, superseded = [], 0, 0, {}, None
    for e in events:
        if e["kind"] != "created":
            if e["cause_source_id"] and not readable(e["cause_source_id"]):
                withheld += 1
                continue
            if not _event_visible(e, mode, cutoff):
                after += 1
                continue
            if e["kind"] in SUPERSEDING:
                superseded = e
            elif e["kind"] == "correction_withdrawn":
                active.pop(e["cause"]["withdraws"], None)
            else:
                active[e["cause_key"]] = e["kind"]
        applied.append(_summary(e))
    if superseded is not None:
        status = {"state": "superseded", "reason": superseded["kind"], "superseded_by": superseded["superseded_by"].strip()}
    elif "retracted" in active.values():
        status = {"state": "retracted", "reason": "retracted_by_source"}
    elif active:
        status = {"state": "corrected", "reason": "corrected_by_source"}
    else:
        status = {"state": "ok"}
    return status, applied, after, withheld


def _version_visible(obj, mode, cutoff):
    if mode == RECONSTRUCTION:
        return obj["proof_available_at"] is not None and obj["proof_available_at"] <= cutoff
    return obj["first_seen_at"] <= cutoff and (mode != "operational_replay" or obj["produced_at"] <= cutoff)


def _claim_visible(c, mode, cutoff, when):
    if mode == "current":
        return True
    if mode == "operational_replay":
        return c["first_seen_at"] <= cutoff and when <= cutoff
    return c["proof_available_at"] is not None and c["proof_available_at"] <= cutoff


def unresolved_at(conn, records, mode, cutoff, readable):
    """(source, record, is_demo) -> the record-level corrections asserted, not withdrawn and not resolved to a
    version at the cutoff, whose correcting source is readable now."""
    keys = {(r["source_id"], r["source_record_id"], r["is_demo"]) for r in records}
    if not keys:
        return {}
    rows = [dict(r) for r in conn.execute(text("""
        SELECT c.*, res.resolved_at FROM derived_measure_correction_claims c
          LEFT JOIN derived_measure_correction_resolutions res ON res.claim_id = c.id
         WHERE c.target_source_id = ANY(:s) AND c.target_record_id = ANY(:r) ORDER BY c.id"""),
        {"s": sorted({k[0] for k in keys}), "r": sorted({k[1] for k in keys})}).mappings()]
    seen = [c for c in rows if readable(c["source_id"]) and _claim_visible(c, mode, cutoff, c["recorded_at"])]
    withdrawn = {(c["target_source_id"], c["target_record_id"], c["relation"], c["origin_version_id"])
                 for c in seen if c["kind"] == "withdrawn"}
    out = {}
    for c in seen:
        key = (c["target_source_id"], c["target_record_id"], c["is_demo"])
        resolved = c["resolved_at"] is not None and (mode != "operational_replay" or c["resolved_at"] <= cutoff)
        if (key in keys and c["kind"] == "asserted" and not resolved
                and (key[0], key[1], c["relation"], c["origin_version_id"]) not in withdrawn):
            out.setdefault(key, []).append({"claim_id": c["id"], "relation": c["relation"],
                                            "by": {"source_id": c["source_id"], "record_id": c["source_record_id"],
                                                   "version_id": c["event_version_id"].strip()}})
    return out


def _not_current(obj, status, mode, checker):
    if status["state"] == "superseded":
        return status["reason"]
    if status.get("reason") == UNKNOWN_TARGET:
        return UNKNOWN_TARGET
    if obj["checker_version"] == NOT_CHECKED:
        return None  # no checker has looked at it: nothing to be out of date
    if mode != "operational_replay" and obj["checker_version"] != checker:
        return "checker_not_current"  # history until the current checker re-checked its reading
    if mode == "operational_replay" and obj["checker_version"] != checker:
        return "checker_history_unknown"  # which checker was in force at the cutoff is not recorded
    return None


def receipt(conn, obj, events, mode, cutoff, readable, checker, unresolved=()):
    """One object as the reader may see it: the MeasurementV2 body with its folded status and identity, and how the
    state at the cutoff came about. A correction of its record that names no version leaves it unknown."""
    status, applied, after, withheld = fold(events, mode, cutoff, readable)
    if unresolved and status["state"] in ("ok", "corrected"):
        status = {"state": "unknown", "reason": UNKNOWN_TARGET}
    body = dict(obj["body"], status=status, identity=identity.of(conn, obj, mode, cutoff, readable))
    do.MeasurementV2.model_validate(body)  # what is served is the contract, never a looser shape
    why = _not_current(obj, status, mode, checker)
    return {"measurement_id": obj["measurement_id"].strip(), "reading_id": obj["reading_id"].strip(), **body,
            "mode": mode, "cutoff": cutoff.isoformat(), "state_at_cutoff": status["state"],
            "events_applied": applied, "events_after_cutoff": after, "events_withheld": withheld,
            "reconstruction": mode == RECONSTRUCTION, "current": why is None, "not_current_reason": why,
            "unresolved_corrections": list(unresolved),
            "versions": {"data_objects": do.CONTRACT_VERSION_V2, "producer": obj["producer_identity"],
                         "checker": obj["checker_version"], "contract": obj["contract_version"],
                         "text_processor": obj["text_processor_version"]}}


def _open(conn, mode, cutoff):
    conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    history._eligibility(mode)  # refuses an unknown mode
    cutoff = history._cutoff(cutoff)
    if cutoff > history._now(conn):
        raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
    return cutoff


_OBJECTS = """SELECT m.*, r.item FROM derived_measurements m JOIN derived_measure_readings r
                ON r.reading_id = m.reading_id WHERE {where} ORDER BY m.version_id, m.measurement_id LIMIT :n"""


def _events(conn, ids):
    rows = conn.execute(text("SELECT * FROM derived_measurement_events WHERE measurement_id = ANY(:ids) "
                             "ORDER BY event_id"), {"ids": ids}).mappings().all()
    out = {}
    for row in rows:
        out.setdefault(row["measurement_id"], []).append(dict(row))
    return out


def for_versions(source_id, is_demo, version_ids, mode, cutoff, *, include_history=False):
    """The objects of visible versions of one source: by default only those current for the mode."""
    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    with history.history_engine().begin() as conn:
        cutoff = _open(conn, mode, cutoff)
        history._source(conn, source_id)
        objs = [dict(r) for r in conn.execute(text(_OBJECTS.format(
            where="m.source_id = :s AND m.is_demo = :d AND m.version_id = ANY(:ids)")),
            {"s": source_id, "d": is_demo, "ids": list(version_ids), "n": MAX_OBJECTS}).mappings()]
        objs = [o for o in objs if _version_visible(o, mode, cutoff)]
        events, readable, checker = _events(conn, [o["measurement_id"] for o in objs]), Readable(conn), expected_checker()
        claims = unresolved_at(conn, objs, mode, cutoff, readable)
        items, excluded = [], {}
        for obj in objs:
            r = receipt(conn, obj, events.get(obj["measurement_id"], []), mode, cutoff, readable, checker,
                        claims.get((obj["source_id"], obj["source_record_id"], obj["is_demo"]), ()))
            if r["current"] or include_history:
                items.append(r)
            else:
                excluded[r["not_current_reason"]] = excluded.get(r["not_current_reason"], 0) + 1
    return {"schema": SCHEMA, "contract_version": do.CONTRACT_VERSION_V2, "source_id": source_id, "is_demo": is_demo,
            "mode": mode, "cutoff": cutoff.isoformat(), "reconstruction": mode == RECONSTRUCTION,
            "versions_used": {"producers": sorted({i["versions"]["producer"] for i in items}),
                              "checkers": sorted({i["versions"]["checker"] for i in items}),
                              "expected_checker": checker},
            "measurements": items, "excluded": excluded}


def read_one(measurement_id, is_demo, mode, cutoff):
    """One fixed object, also when it is superseded or not current; not visible at the cutoff -> 404."""
    if not re.fullmatch(r"[0-9a-f]{64}", measurement_id or ""):
        raise HistoryError("invalid_measurement_id", "测量编号格式无效", 422)
    with history.history_engine().begin() as conn:
        cutoff = _open(conn, mode, cutoff)
        rows = conn.execute(text(_OBJECTS.format(where="m.measurement_id = :m AND m.is_demo = :d")),
                            {"m": measurement_id, "d": is_demo, "n": 1}).mappings().all()
        if not rows:
            raise HistoryError("version_not_found", "该测量对象在此截止时间不可见", 404)
        obj = dict(rows[0])
        history._source(conn, obj["source_id"])
        if not _version_visible(obj, mode, cutoff):
            raise HistoryError("version_not_found", "该测量对象在此截止时间不可见", 404)
        readable = Readable(conn)
        claims = unresolved_at(conn, [obj], mode, cutoff, readable)
        return receipt(conn, obj, _events(conn, [measurement_id]).get(obj["measurement_id"], []), mode, cutoff,
                       readable, expected_checker(),
                       claims.get((obj["source_id"], obj["source_record_id"], obj["is_demo"]), ()))


def changes(is_demo, *, after_seq=0, limit=50):
    """Events after a sequence number, in commit order. An event whose source (or cause source) is not readable now
    is withheld - counted per source, never shown; next_after is the last sequence looked at."""
    limit = max(1, min(int(limit), MAX_CHANGES))
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        rows = conn.execute(text(
            "SELECT * FROM derived_measurement_events WHERE is_demo=:d AND published_seq>:a ORDER BY published_seq "
            "LIMIT :n"), {"d": is_demo, "a": after_seq, "n": limit + 1}).mappings().all()
        page, readable = rows[:limit], Readable(conn)
        items, withheld = [], {}
        for e in page:
            blocked = next((s for s in (e["source_id"], e["cause_source_id"]) if s and not readable(s)), None)
            if blocked:
                withheld[blocked] = withheld.get(blocked, 0) + 1
                continue
            items.append({"measurement_id": e["measurement_id"].strip(), "version_id": e["version_id"].strip(),
                          "source_id": e["source_id"], **_summary(e), "versions": e["versions"]})
    return {"schema": CHANGES_SCHEMA, "is_demo": is_demo, "after_seq": after_seq,
            "next_after": page[-1]["published_seq"] if page else after_seq, "more": len(rows) > limit,
            "items": items, "withheld": withheld,
            "note": "withheld events belong to sources not readable now; re-read those versions once they are"}
