"""The source list Fabric publishes (GET /api/history/sources): descriptions from the code's declarations, licence state
from the History library, capabilities computed from both.

The library row is the administrator's decision (read_enabled, the dependency sources, access, published_seq); the
descriptive text is the declaration, so a changed sentence in the code is what /sources says at once. An old row's
`catalog` jsonb stays in the table as the snapshot taken when it was seeded and is not read for the description of a
declared source.

Where a source goes: readable, not retired -> `sources`; retired (by the code's declaration) -> `retired_sources`
(readable or not, with the decision and effective times of its lifecycle); declared but not readable now ->
`unavailable_sources`, so the consumer can see `readable: false` for it. `DATAFABRIC_RETIRED_IN_SOURCES` (a list, for the
window in which an older consumer still reads only `sources`) repeats the named retired sources in `sources` too; it
changes the position only, never what the source can do.
"""

from __future__ import annotations

import os

from sqlalchemy import text

from biz_rules import source_capabilities as caps
from biz_rules.history_models import HistoryError


def retired_in_sources() -> set[str]:
    return {item.strip() for item in os.environ.get("DATAFABRIC_RETIRED_IN_SOURCES", "").split(",") if item.strip()}


def description(row) -> dict:
    """The catalog text of a source: its declaration; for a source the code does not declare, the row's own snapshot."""
    from biz_rules.retired_sources import RETIRED
    from biz_rules.source_licenses import DERIVED, collected_licenses

    declared = collected_licenses().get(row["source_id"]) or next(
        (item for item in DERIVED if item.source_id == row["source_id"]), None)
    if declared is not None:
        base = declared.catalog_entry()
    elif row["source_id"] in RETIRED:
        base = RETIRED[row["source_id"]].catalog_entry()
    else:
        return dict(row["catalog"])
    # the administrator's decisions in the row win over the declaration's defaults
    for key in ("access", "dependency_source_ids"):
        if key in row["catalog"]:
            base[key] = row["catalog"][key]
    return base


def lifecycle(conn, source_ids) -> dict[str, dict]:
    """source -> {state, decided_at, effective_at, decision_ref}; from the append-only table, newest event decides."""
    result = {source_id: {"state": "none", "decided_at": None, "effective_at": None, "decision_ref": None}
              for source_id in source_ids}
    if not source_ids or conn.execute(text("SELECT to_regclass('history_source_lifecycle')")).scalar_one() is None:
        return result
    events = conn.execute(text(
        "SELECT source_id, event, at, at_precision, decision_ref FROM history_source_lifecycle "
        "WHERE source_id = ANY(:ids) ORDER BY event_id"), {"ids": list(source_ids)}).mappings()
    for event in events:
        info = result[event["source_id"]]
        info["decision_ref"] = event["decision_ref"]
        if event["event"] == "retirement_decided":
            info["decided_at"] = {"at": event["at"].isoformat(), "precision": event["at_precision"]}
        elif event["event"] == "retirement_effective":
            info["effective_at"] = event["at"].isoformat()
        elif event["event"] == "retirement_withdrawn":
            info["effective_at"] = None
        info["state"] = event["event"].removeprefix("retirement_")
    return result


def index_facts() -> tuple[dict[str, list[dict]], str | None]:
    """The configured index targets by source; a broken configuration is reported, not raised (the list still works)."""
    from db import index_publication

    try:
        configured = index_publication.targets()
    except HistoryError as exc:
        return {}, exc.code
    by_source: dict[str, list[dict]] = {}
    for target in configured:
        by_source.setdefault(target.source_id, []).append({"scope": target.source_scope, "is_demo": target.is_demo})
    return {key: sorted(value, key=lambda t: (t["scope"], t["is_demo"])) for key, value in by_source.items()}, None


def listing(rows, lifecycle_by_source, allowed, dependencies_of) -> dict:
    """The receipt, from the library rows. `dependencies_of(row)` is db.history._catalog_dependencies."""
    from biz_rules.history_sources import SOURCE_CATALOG, public_fetch_enabled
    from biz_rules.retired_sources import RETIRED

    by_id = {row["source_id"]: row for row in rows}

    def enabled(source_id, seen=()):
        row = by_id.get(source_id)
        if row is None or source_id in seen or source_id not in allowed or row["read_enabled"] is not True:
            return False
        return all(enabled(value, (*seen, source_id)) for value in dependencies_of(row))

    targets, index_error = index_facts()
    fetch_enabled = public_fetch_enabled()
    in_sources, retired, readable_ids = [], [], set()
    for row in rows:
        source_id = row["source_id"]
        readable = enabled(source_id)
        facts = caps.SourceFacts(source_id, readable, int(row["published_seq"] or 0),
                                 tuple(targets.get(source_id, ())), index_error, fetch_enabled)
        if readable:
            readable_ids.add(source_id)
        entry = {"source_id": source_id, **description(row), "published_seq": row["published_seq"],
                 "read_enabled": True, "academic_use_only": True, "capabilities": caps.capabilities(facts)}
        if source_id in RETIRED:
            retired.append(_retired_entry(row, entry, lifecycle_by_source[source_id], readable))
        if readable and (source_id not in RETIRED or source_id in retired_in_sources()):
            in_sources.append(entry)
    shown = {row["source_id"] for row in rows if enabled(row["source_id"])}
    absent = sorted(set(SOURCE_CATALOG) - shown - set(RETIRED))
    receipt = {"capabilities_version": caps.CAPABILITIES_VERSION, "definition_sha256": caps.definition_sha256(),
               "sources": in_sources, "retired_sources": retired,
               "unavailable_sources": [caps.unavailable(source_id) for source_id in absent]}
    receipt["receipt_sha256"] = caps.receipt_sha256(receipt)
    receipt["readable_source_ids"] = sorted(readable_ids)
    return receipt


def _retired_entry(row, entry, life, readable) -> dict:
    from biz_rules.retired_sources import RETIRED

    item = RETIRED[row["source_id"]]
    return {"source_id": row["source_id"], "name": entry.get("name"), "readable": readable,
            "resolves_via": item.resolves_via, "published_seq": row["published_seq"],
            "retirement": {"state": life["state"], "decision_date": item.decision_date,
                           "decided_at": life["decided_at"], "effective_at": life["effective_at"],
                           "decision_ref": life["decision_ref"] or item.decision_ref},
            "capabilities": entry["capabilities"]}
