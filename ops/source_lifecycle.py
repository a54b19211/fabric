"""Record that a source's retirement took effect (or was withdrawn) in this deployment.

    python -m ops.source_lifecycle effective legacy_documents --actor NAME      # at the moment it runs
    python -m ops.source_lifecycle withdrawn legacy_documents --actor NAME      # rollback: a new event, no row removed

The effective time is the database clock at the moment of the call: it is never taken from the decision date and an
earlier time cannot be given. The decision itself was recorded by migration V036.
"""

import argparse
import json

from sqlalchemy import text

from fetch.retired_sources import RETIRED
from store.history_models import HistoryError

EVENTS = {"effective": "retirement_effective", "withdrawn": "retirement_withdrawn"}


def record(conn, source_id: str, kind: str, *, actor: str) -> dict:
    if source_id not in RETIRED:
        raise HistoryError("not_a_retired_source", f"{source_id} is not retired by the code's declaration", 422)
    conn.execute(text("LOCK TABLE history_source_lifecycle IN SHARE ROW EXCLUSIVE MODE"))
    events = [row[0] for row in conn.execute(text(
        "SELECT event FROM history_source_lifecycle WHERE source_id=:s ORDER BY event_id"), {"s": source_id})]
    if "retirement_decided" not in events:
        raise HistoryError("retirement_not_decided", f"{source_id} has no recorded retirement decision", 409)
    effective = bool(events) and events[-1] == "retirement_effective"
    if kind == "effective" and effective:
        raise HistoryError("retirement_already_effective", f"{source_id} retirement is already in effect", 409)
    if kind == "withdrawn" and not effective:
        raise HistoryError("retirement_not_effective", f"{source_id} retirement is not in effect", 409)
    decision_ref = conn.execute(text(
        "SELECT decision_ref FROM history_source_lifecycle WHERE source_id=:s AND event='retirement_decided' "
        "ORDER BY event_id DESC LIMIT 1"), {"s": source_id}).scalar_one()
    row = conn.execute(text(
        "INSERT INTO history_source_lifecycle(source_id, event, at, at_precision, decision_ref, actor) "
        "VALUES (:s, :e, clock_timestamp(), 'instant', :r, :a) RETURNING event_id, at"),
        {"s": source_id, "e": EVENTS[kind], "r": decision_ref, "a": actor}).mappings().one()
    return {"source_id": source_id, "event": EVENTS[kind], "event_id": row["event_id"], "at": row["at"].isoformat()}


def main(argv=None) -> int:
    from store.history import history_engine

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=sorted(EVENTS))
    parser.add_argument("source_id")
    parser.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    with history_engine().begin() as conn:
        print(json.dumps(record(conn, args.source_id, args.kind, actor=args.actor), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
