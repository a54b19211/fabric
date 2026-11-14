"""测量对象的两个读接口（GET /api/fabric/measurements 和 /measurements/changes，雷达 series_sync 读）。

每次读都先查来源此刻的许可。对象的状态由它的事件在截止时间折叠出来：没有取代事件是 ok（current），
被新运行取代是 superseded（不再 current，superseded_by 指向接替它的对象，没有就为空）。
对象自己说核验结果（check.status，现在一律 unchecked）；它和对象的状态是两件事。

读取模式：current 看全部事件；operational_replay 只看截止时间前生成的对象和事件（截止时系统实际给出的）；
public_history_reconstruction 只看截止时间前有公开证明的版本。
"""

from __future__ import annotations

import re

from sqlalchemy import text

from clean.measure_runs import SUPERSEDING, readable, versions_of
from store import records
from store.record_models import FabricError

SCHEMA, CHANGES_SCHEMA = "datafabric.measurements.v1", "datafabric.measurement_changes.v1"
MAX_OBJECTS, MAX_CHANGES = 2000, 200


def _object_visible(obj, mode, cutoff):
    if mode == "public_history_reconstruction":
        return obj["proof_available_at"] is not None and obj["proof_available_at"] <= cutoff
    return obj["first_seen_at"] <= cutoff and (mode != "operational_replay" or obj["produced_at"] <= cutoff)


def _event_visible(e, mode, cutoff):
    return mode != "operational_replay" or (e["produced_at"] <= cutoff and e["material_visible_at"] <= cutoff)


def _summary(e):
    out = {"kind": e["kind"], "seq": e["published_seq"], "produced_at": e["produced_at"].isoformat(),
           "effective_at": e["effective_at"].isoformat(), "material_visible_at": e["material_visible_at"].isoformat()}
    if e["superseded_by"]:
        out["superseded_by"] = e["superseded_by"].strip()
    return out


def receipt(obj, events, mode, cutoff):
    """One object as a reader sees it at the cutoff: its stored body, its status folded from the events visible then,
    and what produced it."""
    status, applied = {"state": "ok"}, []
    for e in events:
        if e["kind"] != "created" and not _event_visible(e, mode, cutoff):
            continue
        applied.append(_summary(e))
        if e["kind"] in SUPERSEDING:
            status = {"state": "superseded", "reason": e["kind"],
                      "superseded_by": e["superseded_by"].strip() if e["superseded_by"] else None}
    current = status["state"] == "ok"
    return {"measurement_id": obj["measurement_id"].strip(), "reading_id": obj["reading_id"].strip(), **obj["body"],
            "status": status, "state_at_cutoff": status["state"], "current": current,
            "not_current_reason": None if current else status["reason"], "events_applied": applied,
            "versions": versions_of(obj)}


def _open(conn, cutoff):
    conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    if cutoff.tzinfo is None:
        raise FabricError("invalid_cutoff", "截止时间必须包含时区", 422)
    if cutoff > conn.execute(text("SELECT clock_timestamp()")).scalar_one():
        raise FabricError("future_cutoff", "证据截止时间不能晚于服务器当前时间")


def for_versions(source_id, is_demo, version_ids, mode, cutoff, *, include_history=False):
    """The objects of these versions of one source visible at the cutoff: the current ones, with include_history
    also those superseded."""
    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise FabricError("query_budget", "一次最多 100 个合法版本号", 422)
    with records.fabric_engine().begin() as conn:
        _open(conn, cutoff)
        records._source(conn, source_id)  # 403 when the source may not be read now
        objs = [dict(r) for r in conn.execute(text(
            "SELECT * FROM derived_measurements WHERE source_id=:s AND is_demo=:d AND version_id=ANY(:ids) "
            "ORDER BY version_id, measurement_id LIMIT :n"),
            {"s": source_id, "d": is_demo, "ids": list(version_ids), "n": MAX_OBJECTS}).mappings()]
        objs = [o for o in objs if _object_visible(o, mode, cutoff)]
        events = {}
        for e in conn.execute(text("SELECT * FROM derived_measurement_events WHERE measurement_id=ANY(:ids) "
                                   "ORDER BY event_id"), {"ids": [o["measurement_id"] for o in objs]}).mappings():
            events.setdefault(e["measurement_id"], []).append(dict(e))
    items = [receipt(o, events.get(o["measurement_id"], []), mode, cutoff) for o in objs]
    return {"schema": SCHEMA, "source_id": source_id, "is_demo": is_demo, "mode": mode, "cutoff": cutoff.isoformat(),
            "measurements": [r for r in items if r["current"] or include_history]}


def changes(is_demo, *, after_seq=0, limit=50):
    """The events after a sequence number, in commit order. An event of a source not readable now is withheld
    (counted per source, never shown); next_after is the last sequence looked at."""
    limit = max(1, min(int(limit), MAX_CHANGES))
    with records.fabric_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        rows = conn.execute(text(
            "SELECT * FROM derived_measurement_events WHERE is_demo=:d AND published_seq>:a ORDER BY published_seq "
            "LIMIT :n"), {"d": is_demo, "a": after_seq, "n": limit + 1}).mappings().all()
        page, allowed, items, withheld = rows[:limit], {}, [], {}
        for e in page:
            source = e["source_id"]
            if source not in allowed:
                allowed[source] = readable(conn, source)
            if not allowed[source]:
                withheld[source] = withheld.get(source, 0) + 1
                continue
            items.append({"measurement_id": e["measurement_id"].strip(), "version_id": e["version_id"].strip(),
                          "source_id": source, **_summary(e), "versions": e["versions"]})
    return {"schema": CHANGES_SCHEMA, "is_demo": is_demo, "after_seq": after_seq,
            "next_after": page[-1]["published_seq"] if page else after_seq, "more": len(rows) > limit,
            "items": items, "withheld": withheld}
