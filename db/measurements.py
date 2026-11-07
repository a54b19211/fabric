"""Checked measurement objects of the processing layer and their append-only history.

- publish(): after every batch of a measures check run completed, each reading the checker found consistent, whose
  values are exact and actual and whose period is stated, becomes one object (contracts.data_objects.MeasurementV2;
  measurement_id = digest of reading and checker version) with a "created" event. The same reading checked by a
  newer checker gets a new object; the older one gets "rechecked_by_checker" (until then it is not current, see
  db.measurement_reads). A run that supersedes another (a clean-text upgrade, or an explicit producer upgrade)
  supersedes each old object it read again - same quote, value and metric - with "superseded_by_processor" /
  "superseded_by_producer". Corrections already known for the version are applied to the new objects.
- relocate(): when a clean-text upgrade's run is read, each old reading that has an object and was not read again is
  relocated (its quote once in the new text: a reading built from the old measure, checked without a model call),
  owed (several places, or literals no longer located) or gone (quote not in the new text); the old object stays.
- scan_corrections(): bounded scans of every source's versions in published order record the corrections and
  retractions they state (and the withdrawals a later version of the correcting record makes); every object of the
  target version gets the event, with the correcting version's visibility as material and effective time. A
  relation that names the target record but no version is kept as a claim on the record (the record's objects read
  as unknown until resolve_correction() names the version); rescan_corrections() sets a source's cursor back so that
  improved rules see old versions again (the scan records nothing twice).
Events carry three times: material_visible_at (the material that caused it), produced_at (Fabric's result) and
effective_at (corrections: when the material became visible; Fabric's own supersessions: when produced).
"""

from __future__ import annotations

import json
import re

from sqlalchemy import text

from contracts import data_objects as do
from processing import measure_extract as mx
from processing import measure_rules as rules

SUPERSEDING = ("superseded_by_processor", "superseded_by_producer", "rechecked_by_checker")
CORRECTING = ("corrected", "retracted", "correction_withdrawn")
CORRECTION_EVENT = {"corrects": "corrected", "retracts": "retracted"}
SCAN_BATCH = 500


def literal_key(source_id, record_id, value):
    """The record-local key of an object without a confirmed identity: same name elsewhere is another key."""
    return "literal:" + source_id + "|" + record_id + "|" + " ".join((value or "").split()).casefold()


def measurement_id(reading_id, checker_version):
    return mx.sha256({"reading_id": reading_id, "checker_version": checker_version})


# ---- events ---------------------------------------------------------------------------------------------------


def event(conn, obj, kind, cause_key, cause, *, visible_at, proof_at=None, cause_source_id=None, superseded_by=None,
          effective="produced"):
    """Append one event of an object (idempotent by object, kind and cause). The feed sequence is taken under the
    feed row's lock, held to commit: the feed order is the commit order."""
    if conn.execute(text("SELECT 1 FROM derived_measurement_events WHERE measurement_id=:m AND kind=:k "
                         "AND cause_key=:c"), {"m": obj["measurement_id"], "k": kind, "c": cause_key}).first():
        return False
    seq = conn.execute(text(
        "INSERT INTO derived_measurement_feed(is_demo,last_seq) VALUES (:d,1) ON CONFLICT (is_demo) "
        "DO UPDATE SET last_seq=derived_measurement_feed.last_seq+1 RETURNING last_seq"),
        {"d": obj["is_demo"]}).scalar_one()
    versions = {"data_objects": do.CONTRACT_VERSION_V2, "producer": obj["producer_identity"],
                "checker": obj["checker_version"], "contract": obj["contract_version"],
                "text_processor": obj["text_processor_version"]}
    conn.execute(text("""
        INSERT INTO derived_measurement_events(is_demo,published_seq,measurement_id,source_id,version_id,kind,
          cause_key,cause,cause_source_id,superseded_by,material_visible_at,material_proof_at,produced_at,
          effective_at,versions)
        SELECT :d,:seq,:m,:s,:v,:k,:ck,CAST(:cause AS jsonb),:cs,:by,:vis,:proof,now.t,
               CASE WHEN :eff='material' THEN CAST(:vis AS timestamptz) ELSE now.t END,CAST(:versions AS jsonb)
          FROM (SELECT clock_timestamp() AS t) now
        ON CONFLICT DO NOTHING"""),
        {"d": obj["is_demo"], "seq": seq, "m": obj["measurement_id"], "s": obj["source_id"], "v": obj["version_id"],
         "k": kind, "ck": cause_key, "cause": json.dumps(cause), "cs": cause_source_id, "by": superseded_by,
         "vis": visible_at, "proof": proof_at, "eff": effective, "versions": json.dumps(versions)})
    return True


# ---- objects ----------------------------------------------------------------------------------------------------


def _gate(reading, answer):
    """Why a checked reading is not an object (None when it is one)."""
    final = reading["final"]
    if answer.get("status") != "consistent":
        return "not_consistent"
    if final["qualifier"] != "exact" or final["modality"] != "actual":
        return "approximate_or_not_actual"
    if not final.get("period"):
        return "period_not_stated"  # a measurement states its period (data_objects v2)
    return None


def body_of(reading, answer, version, check):
    """The MeasurementV2 body of a checked reading (status ok, record-local identity: both are read, not stored)."""
    final, item, measure = reading["final"], reading["item"], reading["measure"]
    dims_answer = answer.get("dims") or {}
    sha, vid = reading["text_sha256"], reading["version_id"].strip()
    inputs = [do.VersionRef(source_id=version["source_id"], record_id=version["source_record_id"], version_id=vid)]
    denominator = final.get("denominator")
    body = do.MeasurementV2(
        inputs=inputs,
        processing=do.Processing(processor_version=mx.PROCESSOR_VERSION, config_sha256=reading["model_config_sha256"]),
        locators=[do.Locator(kind="char_span", version_id=vid, start=reading["quote_start"], end=reading["quote_end"],
                             derived_text_sha256=sha)],
        permission_sources=[version["source_id"]],
        visibility=do.merge_visibility(inputs, {vid: (version["first_seen_at"], version["proof_available_at"])}),
        status=do.Status(state="ok"),
        artifact=do.ArtifactRef(text_processor_version=reading["text_processor_version"], text_sha256=sha),
        object=final["object"] or "unknown", metric=final["metric"], value_text=final["value"],
        value_literal=measure["value_text"], unit=final["unit"] or "unknown", period=final["period"],
        period_literal=measure.get("period_text") or None, scope=final["scope"] or None,
        scope_mode=final["scope_mode"], denominator_text=denominator,
        denominator_state="stated" if denominator else "unknown" if final["measure_kind"] == "rate" else
        "not_applicable",
        measure_kind=final["measure_kind"], qualifier=final["qualifier"], modality=final["modality"],
        family=item.get("family") or "",
        dims=[do.MeasureDim(kind=d["kind"], value=d["value"], start=d["span"]["start"], end=d["span"]["end"],
                            check=(dims_answer.get(d["kind"]) or {}).get("result", "unchecked"))
              for d in item.get("dims") or []],
        dims_unlocated=[do.UnlocatedDim(kind=d.get("kind"), text=d.get("text") or "", reason=d["reason"])
                        for d in final.get("dims_unlocated") or []],
        check=do.CheckRef(status=answer["status"], **check),
        quote=item["quote"],
        identity=do.MeasureIdentity(state="provisional",
                                    key=literal_key(version["source_id"], version["source_record_id"], final["object"])))
    return body.model_dump(mode="json")


_READINGS = """
    SELECT r.*, run.model_config_sha256, run.supersedes_run_id FROM derived_measure_readings r
      JOIN derived_measure_runs run ON run.run_id = r.run_id WHERE r.reading_id = ANY(:ids)
"""


def _version(conn, version_id):
    return conn.execute(text(
        "SELECT version_id,source_id,source_record_id,is_demo,first_seen_at,proof_available_at FROM history_versions "
        "WHERE version_id=:v"), {"v": version_id}).mappings().first()


def _insert(conn, reading, check_row, version, check):
    """The object of one checked reading; (object row, created now) or (None, False) when it is not one."""
    answer = check_row["items"][0]
    if _gate(reading, answer):
        return None, False
    mid = measurement_id(reading["reading_id"], check["checker_version"])
    body = body_of(reading, answer, version, check)
    made = conn.execute(text("""
        INSERT INTO derived_measurements(measurement_id,reading_id,run_id,version_id,source_id,source_record_id,
          is_demo,text_processor_version,text_sha256,producer_identity,checker_version,contract_version,check_run_id,
          check_row_id,body,first_seen_at,proof_available_at)
        VALUES (:m,:r,:run,:v,:s,:rec,:d,:tpv,:sha,:pi,:c,:cv,:cr,:row,CAST(:body AS jsonb),:seen,:proof)
        ON CONFLICT DO NOTHING"""),
        {"m": mid, "r": reading["reading_id"], "run": reading["run_id"], "v": reading["version_id"],
         "s": version["source_id"], "rec": version["source_record_id"], "d": version["is_demo"],
         "tpv": reading["text_processor_version"], "sha": reading["text_sha256"], "pi": reading["producer_identity"],
         "c": check["checker_version"], "cv": check["contract_version"], "cr": check["run_id"], "row": check_row["id"],
         "body": json.dumps(body, ensure_ascii=False), "seen": version["first_seen_at"],
         "proof": version["proof_available_at"]}).rowcount
    obj = conn.execute(text("SELECT * FROM derived_measurements WHERE measurement_id=:m"), {"m": mid}).mappings().first()
    return dict(obj), bool(made)


def publish(conn, task, check_run_id, reading_ids):
    """Objects for the readings of one completed measures check run (batch i checked reading_ids[i]). Returns the
    ids of the objects created now."""
    rows = conn.execute(text(
        "SELECT id,batch_index,items,checker_version,contract_version FROM derived_extraction_checks "
        "WHERE run_id=:r AND batch_index>=0 AND status='completed' ORDER BY batch_index"),
        {"r": check_run_id}).mappings().all()
    readings = {r["reading_id"]: dict(r) for r in conn.execute(text(_READINGS), {"ids": list(reading_ids)}).mappings()}
    version = _version(conn, task["version_id"])
    made = []
    for row in rows:
        reading = readings[reading_ids[row["batch_index"]]]
        check = {"checker_version": row["checker_version"], "contract_version": row["contract_version"],
                 "run_id": check_run_id}
        obj, created = _insert(conn, reading, row, version, check)
        if obj is None or not created:
            continue
        event(conn, obj, "created", "created", {"check_run_id": check_run_id}, visible_at=version["first_seen_at"],
              proof_at=version["proof_available_at"])
        _rechecked(conn, obj)
        made.append(obj["measurement_id"])
    for run_id in {r["run_id"] for r in readings.values()}:
        supersede_run(conn, run_id)
        # objects published late for a run that was already superseded are history at once, never current
        for (later,) in conn.execute(text("SELECT run_id FROM derived_measure_runs WHERE supersedes_run_id=:r"),
                                     {"r": run_id}):
            supersede_run(conn, later)
    correct_version(conn, task["version_id"])
    return made


def _rechecked(conn, obj):
    """The same reading's objects of other checkers are history once a newer check made this one."""
    for old in conn.execute(text("SELECT * FROM derived_measurements WHERE reading_id=:r AND measurement_id<>:m "
                                 "AND produced_at<:t"),
                            {"r": obj["reading_id"], "m": obj["measurement_id"], "t": obj["produced_at"]}).mappings():
        event(conn, dict(old), "rechecked_by_checker", "checker:" + obj["checker_version"],
              {"checker_version": obj["checker_version"], "measurement_id": obj["measurement_id"]},
              visible_at=old["first_seen_at"], proof_at=old["proof_available_at"],
              superseded_by=obj["measurement_id"])


def _match_key(item):
    return item["quote"], item["fields"]["value"].get("value"), item["fields"]["metric"].get("value")


def supersede_run(conn, run_id):
    """Each object of the superseded run that this run read again (same quote, value, metric) is superseded by this
    run's object: by processor when the clean artifact differs, by producer when only the producer does."""
    run = conn.execute(text("SELECT * FROM derived_measure_runs WHERE run_id=:r"), {"r": run_id}).mappings().first()
    if run is None or not run["supersedes_run_id"]:
        return 0
    old = conn.execute(text("SELECT * FROM derived_measure_runs WHERE run_id=:r"),
                       {"r": run["supersedes_run_id"]}).mappings().first()
    kind = ("superseded_by_processor" if (old["text_processor_version"], old["text_sha256"]) !=
            (run["text_processor_version"], run["text_sha256"]) else "superseded_by_producer")
    objects = """SELECT m.*, r.item FROM derived_measurements m JOIN derived_measure_readings r
                   ON r.reading_id = m.reading_id WHERE m.run_id = :r"""
    new = {}
    for row in conn.execute(text(objects), {"r": run_id}).mappings():
        new.setdefault(_match_key(row["item"]), dict(row))
    n = 0
    for row in conn.execute(text(objects), {"r": old["run_id"]}).mappings():
        by = new.get(_match_key(row["item"]))
        if by is not None and event(conn, dict(row), kind, "run:" + run_id, {"run_id": run_id},
                                    visible_at=row["first_seen_at"], proof_at=row["proof_available_at"],
                                    superseded_by=by["measurement_id"]):
            n += 1
    return n


# ---- relocation after a clean-text upgrade --------------------------------------------------------------------


def _relocated(conn, run, full, windows, old):
    """(outcome, reason, new reading id, occurrences) for one old reading in the new artifact."""
    quote = old["item"]["quote"]
    hits = [x.start() for x in re.finditer(re.escape(quote), full)] if quote else []
    if not hits:
        return "gone", "quote_not_in_new_text", None, 0
    if len(hits) > 1:
        return "owed", "quote_in_several_places", None, len(hits)
    w = next((w for w in windows if w["start_char"] <= hits[0] and hits[0] + len(quote) <= w["end_char"]), None)
    if w is None:
        return "owed", "quote_in_no_window", None, 1
    from db import measure_runs

    window = {**dict(run), "window_index": w["window_index"], "start_char": w["start_char"], "end_char": w["end_char"]}
    sent = rules.Sent((w["start_char"], w["end_char"]),
                      {k: v for k, v in rules.contexts_of(full, w["start_char"]).items() if v})
    drafts, not_built = mx.readings_of([old["measure"]], full, sent, measure_runs._subject(window),
                                       measure_runs.keys_of(window, run["model_config_sha256"]))
    if not drafts:
        return "owed", not_built[0]["reason"], None, 1
    measure_runs.insert_readings(conn, window, drafts, relocated_from=old["reading_id"])
    return "relocated", None, drafts[0].reading_id, 1


def relocate(conn, run):
    """Where each object-bearing reading of the superseded run goes in this run's (new) artifact. Written once per
    (old reading, new run); a producer upgrade on the same artifact relocates nothing."""
    if not run.get("supersedes_run_id"):
        return 0
    old_run = conn.execute(text("SELECT * FROM derived_measure_runs WHERE run_id=:r"),
                           {"r": run["supersedes_run_id"]}).mappings().first()
    if old_run is None or (old_run["text_processor_version"], old_run["text_sha256"]) == (
            run["text_processor_version"], run["text_sha256"]):
        return 0
    full = conn.execute(text(
        "SELECT text FROM derived_clean_text WHERE version_id=:v AND processor_version=:tpv AND text_sha256=:sha "
        "AND status='parsed' ORDER BY attempt DESC LIMIT 1"),
        {"v": run["version_id"], "tpv": run["text_processor_version"], "sha": run["text_sha256"]}).scalar()
    if full is None:
        return 0
    reread = {_match_key(r["item"]): r["reading_id"] for r in conn.execute(text(
        "SELECT reading_id,item FROM derived_measure_readings WHERE run_id=:r"), {"r": run["run_id"]}).mappings()}
    windows = conn.execute(text("SELECT window_index,start_char,end_char FROM derived_measure_windows WHERE run_id=:r "
                                "ORDER BY window_index"), {"r": run["run_id"]}).mappings().all()
    olds = conn.execute(text("""
        SELECT r.* FROM derived_measure_readings r WHERE r.run_id=:old
           AND EXISTS (SELECT 1 FROM derived_measurements m WHERE m.reading_id=r.reading_id)
           AND NOT EXISTS (SELECT 1 FROM derived_measure_relocations x WHERE x.old_reading_id=r.reading_id
                             AND x.new_run_id=:new)"""), {"old": old_run["run_id"], "new": run["run_id"]}).mappings()
    n = 0
    for old in [dict(r) for r in olds]:
        again = reread.get(_match_key(old["item"]))
        outcome = ("reread", None, again, 1) if again else _relocated(conn, run, full, windows, old)
        conn.execute(text("INSERT INTO derived_measure_relocations(old_reading_id,new_run_id,outcome,reason,"
                          "new_reading_id,quote_occurrences) VALUES (:o,:r,:out,:why,:new,:n) ON CONFLICT DO NOTHING"),
                     {"o": old["reading_id"], "r": run["run_id"], "out": outcome[0], "why": outcome[1],
                      "new": outcome[2], "n": outcome[3]})
        n += 1
    return n


# ---- corrections --------------------------------------------------------------------------------------------------


def _relations(payload):
    rels = ((payload or {}).get("document_evidence") or {}).get("document_relations") or []
    return {(r["target_version_id"], r["relation_type"]) for r in rels
            if r.get("relation_type") in CORRECTION_EVENT and r.get("target_version_id")}


def _record_relations(payload):
    """(target source, target record, relation) of the corrections that name no target version."""
    rels = ((payload or {}).get("document_evidence") or {}).get("document_relations") or []
    return {(r["target_source_id"], r["target_source_record_id"], r["relation_type"]) for r in rels
            if r.get("relation_type") in CORRECTION_EVENT and not r.get("target_version_id")}


def _claim(conn, row, key, kind, origin):
    conn.execute(text("""
        INSERT INTO derived_measure_correction_claims(event_version_id,target_source_id,target_record_id,relation,kind,
          origin_version_id,source_id,source_record_id,is_demo,first_seen_at,proof_available_at)
        VALUES (:e,:ts,:tr,:rel,:k,:o,:s,:rec,:d,:seen,:proof) ON CONFLICT DO NOTHING"""),
        {"e": row["version_id"], "ts": key[0], "tr": key[1], "rel": key[2], "k": kind, "o": origin,
         "s": row["source_id"], "rec": row["source_record_id"], "d": row["is_demo"], "seen": row["first_seen_at"],
         "proof": row["proof_available_at"]})


def _scan_claims(conn, row):
    """The record-level claims one version states and the withdrawals of earlier ones; returns the (target, relation)
    pairs that resolutions of claims it still states made, so that their links are not taken as withdrawn."""
    stated = _record_relations(row["payload"])
    for key in stated:
        _claim(conn, row, key, "asserted", row["version_id"])
    earlier = conn.execute(text("""
        SELECT DISTINCT c.target_source_id, c.target_record_id, c.relation, c.event_version_id, r.target_version_id
          FROM derived_measure_correction_claims c
          LEFT JOIN derived_measure_correction_resolutions r ON r.claim_id = c.id
         WHERE c.source_id=:s AND c.source_record_id=:rec AND c.is_demo=:d AND c.kind='asserted'
           AND c.first_seen_at<:seen"""),
        {"s": row["source_id"], "rec": row["source_record_id"], "d": row["is_demo"],
         "seen": row["first_seen_at"]}).all()
    kept = set()
    for ts, tr, relation, origin, resolved in earlier:
        if (ts, tr, relation) in stated:
            if resolved:
                kept.add((resolved, relation))
        else:
            _claim(conn, row, (ts, tr, relation), "withdrawn", origin)
    return kept


def rescan_corrections(conn, source_id, after_seq=0):
    """Explicitly scan a source again from after_seq (for example after the relation rules improved)."""
    return conn.execute(text("UPDATE derived_measure_correction_cursor SET last_seq=:a,updated_at=clock_timestamp() "
                             "WHERE source_id=:s"), {"s": source_id, "a": after_seq}).rowcount


def resolve_correction(claim_id, target_version_id, *, actor, basis):
    """Name the version a record-level correction meant (who and on what basis); from then its link and the normal
    corrected / retracted events apply, and its record is no longer unknown."""
    from biz_rules.history_models import HistoryError
    from db import history

    with history.history_engine().begin() as conn:
        claim = conn.execute(text("SELECT * FROM derived_measure_correction_claims WHERE id=:id AND kind='asserted'"),
                             {"id": claim_id}).mappings().first()
        if claim is None:
            raise HistoryError("correction_claim_not_found", "没有这条未落到版本的更正", 404)
        if conn.execute(text("SELECT 1 FROM history_versions WHERE version_id=:v AND source_id=:s AND "
                             "source_record_id=:r AND is_demo=:d"),
                        {"v": target_version_id, "s": claim["target_source_id"], "r": claim["target_record_id"],
                         "d": claim["is_demo"]}).first() is None:
            raise HistoryError("correction_target_not_of_record", "所给版本不属于更正所指的记录", 409)
        made = conn.execute(text("INSERT INTO derived_measure_correction_resolutions(claim_id,target_version_id,actor,"
                                 "basis) VALUES (:c,:t,:a,:b) ON CONFLICT DO NOTHING"),
                            {"c": claim_id, "t": target_version_id, "a": actor, "b": basis}).rowcount
        if not made:
            raise HistoryError("correction_claim_resolved", "这条更正已落到版本", 409)
        asserting = {"version_id": claim["event_version_id"], "source_id": claim["source_id"],
                     "source_record_id": claim["source_record_id"], "is_demo": claim["is_demo"],
                     "first_seen_at": claim["first_seen_at"], "proof_available_at": claim["proof_available_at"]}
        _link(conn, asserting, target_version_id, claim["relation"], "asserted", claim["origin_version_id"])
        return correct_version(conn, target_version_id)


def _link(conn, row, target, relation, kind, origin):
    conn.execute(text("""
        INSERT INTO derived_measure_correction_links(event_version_id,target_version_id,relation,kind,
          origin_version_id,source_id,source_record_id,is_demo,first_seen_at,proof_available_at)
        VALUES (:e,:t,:rel,:k,:o,:s,:rec,:d,:seen,:proof) ON CONFLICT DO NOTHING"""),
        {"e": row["version_id"], "t": target, "rel": relation, "k": kind, "o": origin, "s": row["source_id"],
         "rec": row["source_record_id"], "d": row["is_demo"], "seen": row["first_seen_at"],
         "proof": row["proof_available_at"]})


def _scan_version(conn, row):
    """The links one version states, and the withdrawals of what earlier versions of its record stated. Returns
    the target versions touched."""
    stated = _relations(row["payload"])
    for target, relation in stated:
        _link(conn, row, target, relation, "asserted", row["version_id"])
    stated |= _scan_claims(conn, row)
    earlier = conn.execute(text("""
        SELECT DISTINCT target_version_id, relation, event_version_id FROM derived_measure_correction_links
         WHERE source_id=:s AND source_record_id=:rec AND is_demo=:d AND kind='asserted' AND first_seen_at<:seen"""),
        {"s": row["source_id"], "rec": row["source_record_id"], "d": row["is_demo"],
         "seen": row["first_seen_at"]}).all()
    for target, relation, origin in earlier:
        if (target, relation) not in stated:
            _link(conn, row, target, relation, "withdrawn", origin)
    return {t for t, _ in stated} | {t for t, r, _ in earlier if (t, r) not in stated}


def scan_corrections(conn, source_ids, batch=SCAN_BATCH):
    """One bounded step per source: the next versions in published order. Returns the target versions touched."""
    touched = set()
    for source_id in source_ids:
        last = conn.execute(text("SELECT last_seq FROM derived_measure_correction_cursor WHERE source_id=:s FOR UPDATE"),
                            {"s": source_id}).scalar() or 0
        rows = conn.execute(text(
            "SELECT version_id,source_id,source_record_id,is_demo,first_seen_at,proof_available_at,published_seq,"
            "payload FROM history_versions WHERE source_id=:s AND published_seq>:last ORDER BY published_seq LIMIT :n"),
            {"s": source_id, "last": last, "n": batch}).mappings().all()
        for row in rows:
            touched |= _scan_version(conn, dict(row))
        if rows:
            conn.execute(text(
                "INSERT INTO derived_measure_correction_cursor(source_id,last_seq) VALUES (:s,:q) ON CONFLICT "
                "(source_id) DO UPDATE SET last_seq=EXCLUDED.last_seq,updated_at=clock_timestamp()"),
                {"s": source_id, "q": rows[-1]["published_seq"]})
    for target in touched:
        correct_version(conn, target)
    return touched


def correct_version(conn, target_version_id):
    """Every object of the target version follows every link to it: corrected / retracted from the asserting
    version's visibility on, correction_withdrawn from the withdrawing version's. Returns events appended."""
    links = conn.execute(text("SELECT * FROM derived_measure_correction_links WHERE target_version_id=:t "
                              "ORDER BY first_seen_at, event_version_id"), {"t": target_version_id}).mappings().all()
    if not links:
        return 0
    objects = conn.execute(text("SELECT * FROM derived_measurements WHERE version_id=:t"),
                           {"t": target_version_id}).mappings().all()
    n = 0
    for link in links:
        for obj in objects:
            if obj["is_demo"] != link["is_demo"]:
                continue
            asserted = f"{link['relation']}:{link['origin_version_id']}"
            if link["kind"] == "asserted":
                kind, key, cause = CORRECTION_EVENT[link["relation"]], asserted, {}
            else:
                kind, key, cause = "correction_withdrawn", f"withdrawn:{asserted}:{link['event_version_id']}", {
                    "withdraws": asserted}
            cause = {**cause, "source_id": link["source_id"], "record_id": link["source_record_id"],
                     "version_id": link["event_version_id"].strip(), "relation": link["relation"]}
            n += event(conn, dict(obj), kind, key, cause, visible_at=link["first_seen_at"],
                       proof_at=link["proof_available_at"], cause_source_id=link["source_id"], effective="material")
    return n
