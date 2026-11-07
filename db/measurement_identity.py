"""Who a measurement's object is: only an identity confirmed in a fixed graph package, never a name.

bind() records a binding when the graph package version is readable now, the node's identity is confirmed
(biz_rules.graph_tools.confirmed_identity: an audited review with its basis and evidence, or a native-id rule) and
the node's own evidence points at this measurement's mention - the same fixed version and clean artifact, a span
holding the object literal (or, for a rule identity, a field of that version with the literal as its value). The
same name in another record, an alias or an alignment candidate is no binding.

of() gives the identity a reader may see: the latest binding whose graph source is readable now and that was
visible at the cutoff (operational replay: bound and published by then; public history: the graph package has
no proof of past publication, so no binding); otherwise the record-local literal key with the reason.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from biz_rules.history_models import HistoryError
from db import history


def _mention(node, obj):
    """The node evidence that binds this measurement's object mention, or None."""
    item = obj["item"]
    span = (item["fields"]["object"] or {}).get("span")
    vid, literal = obj["version_id"].strip(), obj["body"]["object"]
    if node.identity_status == "rule_confirmed":
        return next(({"json_pointer": f.json_pointer, "value": f.value} for f in node.field_evidence
                     if f.version_id == vid and str(f.value) == literal), None)
    if span is None:
        return None
    for e in node.evidence:
        loc = e.locator
        if (e.version_id == vid and loc.basis == "derived_clean_text"
                and loc.text_processor_version == obj["text_processor_version"]
                and loc.text_sha256 == obj["text_sha256"].strip()
                and loc.start <= span["start"] and span["end"] <= loc.end):
            return {"quote": e.quote, "locator": loc.model_dump(mode="json")}
    return None


def bind(measurement_id, *, graph_source_id, graph_record_id, graph_version_id, namespace, identity_id):
    """Record that the measurement's object is the confirmed graph identity namespace|identity_id. Refused (409)
    when the identity is not confirmed or its evidence is not this mention."""
    from biz_rules.graph_tools import confirmed_identity
    from biz_rules.history_sources import parse_json
    from biz_rules.reviewed_graph import GraphBundle

    with history.history_engine().begin() as conn:
        obj = conn.execute(text(
            "SELECT m.*, r.item FROM derived_measurements m JOIN derived_measure_readings r ON r.reading_id=m.reading_id "
            "WHERE m.measurement_id=:m"), {"m": measurement_id}).mappings().first()
    if obj is None:
        raise HistoryError("measurement_not_found", "没有这个测量对象", 404)
    record = history.read_version(graph_version_id, graph_source_id, graph_record_id, obj["is_demo"], "current",
                                  datetime.now(timezone.utc))
    if record.record_kind != "graph":
        raise HistoryError("not_a_graph_version", "所选版本不是图包")
    bundle = GraphBundle.model_validate(parse_json(record.raw_content))
    node = next((n for n in bundle.nodes if (n.namespace, n.id) == (namespace, identity_id)), None)
    if node is None or not confirmed_identity(node):
        raise HistoryError("identity_not_confirmed", "图包里没有这个已确认身份（同名或候选不能绑定）", 409)
    evidence = _mention(node, dict(obj))
    if evidence is None:
        raise HistoryError("identity_not_bound_to_mention", "该身份的证据没有指向这条测量的对象提及", 409)
    with history.history_engine().begin() as conn:
        proof = conn.execute(text("SELECT proof_available_at FROM history_versions WHERE version_id=:v"),
                             {"v": graph_version_id}).scalar()
        conn.execute(text("""
            INSERT INTO derived_measurement_identities(measurement_id,role,namespace,identity_id,identity_status,
              graph_source_id,graph_record_id,graph_version_id,graph_first_seen_at,graph_proof_available_at,evidence)
            VALUES (:m,'object',:ns,:id,:st,:gs,:gr,:gv,:seen,:proof,CAST(:ev AS jsonb)) ON CONFLICT DO NOTHING"""),
            {"m": measurement_id, "ns": namespace, "id": identity_id, "st": node.identity_status,
             "gs": graph_source_id, "gr": graph_record_id, "gv": graph_version_id, "seen": record.first_seen_at,
             "proof": proof, "ev": json.dumps(evidence, ensure_ascii=False)})
    return {"measurement_id": measurement_id, "identity": f"{namespace}|{identity_id}",
            "identity_status": node.identity_status, "graph_version_id": graph_version_id}


def _visible(row, mode, cutoff):
    if mode == "current":
        return True
    if mode == "operational_replay":
        return row["produced_at"] <= cutoff and row["graph_first_seen_at"] <= cutoff
    return row["graph_proof_available_at"] is not None and row["graph_proof_available_at"] <= cutoff


def of(conn, obj, mode, cutoff, readable):
    """The identity block of one object for this reader (contracts.data_objects.MeasureIdentity)."""
    literal = obj["body"]["identity"]["key"]
    rows = conn.execute(text("SELECT * FROM derived_measurement_identities WHERE measurement_id=:m ORDER BY id"),
                        {"m": obj["measurement_id"]}).mappings().all()
    if not rows:
        return {"state": "provisional", "key": literal, "reason": "no_confirmed_identity"}
    usable = [r for r in rows if readable(r["graph_source_id"]) and _visible(r, mode, cutoff)]
    if not usable:
        return {"state": "provisional", "key": literal, "reason": "confirmed_identity_not_visible"}
    keys = {(r["namespace"], r["identity_id"]) for r in usable}
    if len(keys) > 1:
        return {"state": "provisional", "key": literal, "reason": "confirmed_identities_conflict"}
    row = usable[-1]
    return {"state": row["identity_status"], "key": f"{row['namespace']}|{row['identity_id']}",
            "graph": {"source_id": row["graph_source_id"], "record_id": row["graph_record_id"],
                      "version_id": row["graph_version_id"].strip()}}
