"""The measurement worker of the processing layer: per round, plan runs for new clean artifacts, read due windows
within the round's model request budget (a spent budget ends the round; the windows wait as budget_spent), resume
runs whose source is readable again and follow the corrections sources state. The BioCheck checks of the readings
are the extraction-check worker's (db.extraction_checks, producer "measures").

Two candidate routes read the windows through this one entry: the model (when DATAFABRIC_MEASURE_LLM_* is set) and the
rules (the explicit counts of who.explicit-count.v1; no model, no request; off with DATAFABRIC_MEASURE_RULES_ROUTE=
false). The standing queue plans every current clean artifact of a measurable source except the versions a bounded
backfill created (flows.who_backfill): background material is measured only when it is listed.

    python -m flows.measure_extract                      # rounds until SIGTERM; disabled without a route
    python -m flows.measure_extract --scope list.json    # once, for the versions of an explicit list only
    python -m flows.measure_extract --scope list.json --upgrade    # the same, for versions older producers read

The list is {"versions": ["<version id>", {"version_id": "<version id>", "windows": [0, 3]}]}: a version alone is read
in all its windows, with "windows" only those reading windows (the others are out_of_scope, not owed). Planning the
list and reading it write the list's digest on every run; the receipt printed says what was planned, what a run
already held and why any version could not be planned. A call reads at most MAX_SCOPE_ROUNDS rounds; a window that
waits out a backoff or a spent provider budget is read by the next call.

A version that already has a run of a route on its artifact is not planned again (the list never enlarges a run). With
--upgrade only a run by the current producer (processor version and model configuration) counts: a listed version that
an older producer read gets a new run by this one, and the old run stays as it was.
"""

from __future__ import annotations

import argparse
import json
import os
import time

from sqlalchemy import text

from db import history, measure_runs, measurements

ROUND_REQUESTS = int(os.environ.get("DATAFABRIC_MEASURE_ROUND_REQUESTS", "50"))
ROUND_WINDOWS = 200
MAX_SCOPE_ROUNDS = 20


def _sources(conn):
    return [r[0] for r in conn.execute(text("SELECT source_id FROM history_sources WHERE read_enabled ORDER BY source_id"))
            if measure_runs._readable(conn, r[0])]


def _plan(model, with_rules, scope, producer_upgrade):
    """(runs planned, the scope's receipt or None): the standing queue, or an explicit list only."""
    if scope is None:
        return measure_runs.plan(model=model, with_rules=with_rules), None
    receipt = measure_runs.plan_scope(scope, model=model, with_rules=with_rules, producer_upgrade=producer_upgrade)
    return sum(len(route["planned"]) for route in receipt["routes"].values()), receipt


def run_round(*, budget_requests=ROUND_REQUESTS, max_windows=ROUND_WINDOWS, model=None, client=None, scope=None,
              with_rules=None, producer_upgrade=False) -> dict:
    """One round; its counts include the model requests it sent (usage) and, for a scope, the plan's receipt
    (producer_upgrade: see plan_scope; it has no meaning without a scope)."""
    model = model or measure_runs.model_from_env()
    with_rules = measure_runs.rules_from_env() if with_rules is None else with_rules
    if model is None and not with_rules:
        return {"state": "disabled"}
    planned, receipt = _plan(model, with_rules, scope, producer_upgrade)
    counts = {"planned": planned, "resumed": measure_runs.resume(), "windows": {}, "budget_spent": False}
    if receipt is not None:
        counts["scope"] = receipt
    budget = measure_runs.RequestBudget(budget_requests)
    for _ in range(max_windows):
        if budget.spent():
            counts["budget_spent"] = True
            break
        state = measure_runs.process_once(model=model, client=client, budget=budget, scope=scope,
                                          with_rules=with_rules)["state"]
        counts["windows"][state] = counts["windows"].get(state, 0) + 1
        if state == "idle":
            break
    counts["requests"] = None if budget_requests is None else budget_requests - budget.left
    with history.history_engine().begin() as conn:
        counts["corrected_versions"] = len(measurements.scan_corrections(conn, _sources(conn)))
    return counts


def run_scope(scope, **kwargs) -> dict:
    """Measure an explicit list: rounds until no window of it is due (at most MAX_SCOPE_ROUNDS). The first round's
    receipt says what was planned; the total of model requests sent is the usage of the call."""
    rounds = []
    for _ in range(MAX_SCOPE_ROUNDS):
        counts = run_round(scope=scope, **kwargs)
        rounds.append(counts)
        if counts.get("state") == "disabled" or counts["windows"].get("idle"):
            break
    return {"scope_sha256": scope.sha256, "rounds": rounds, "requests": sum(r.get("requests") or 0 for r in rounds)}


def run_worker(*, stop, idle_seconds=30.0, rounds=None):
    n = 0
    while not stop.is_set() and (rounds is None or n < rounds):
        n += 1
        counts = run_round()
        if counts.get("state") == "disabled" or counts["windows"].get("idle"):
            stop.wait(idle_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measurement worker, or one explicit list with --scope")
    parser.add_argument("--scope", help="a JSON file listing the versions (and windows) to measure, once")
    parser.add_argument("--upgrade", action="store_true",
                        help="with --scope: also plan a listed version that only older producers read")
    args = parser.parse_args(argv)
    if args.upgrade and not args.scope:
        parser.error("--upgrade needs --scope")
    if measure_runs.model_from_env() is None and not measure_runs.rules_from_env():
        print(json.dumps({"measure_extract": {"state": "disabled"}}))
    elif args.scope:
        with open(args.scope, encoding="utf-8") as f:
            scope = measure_runs.Scope.parse(json.load(f))
        print(json.dumps(run_scope(scope, producer_upgrade=args.upgrade), ensure_ascii=False, default=str))
    else:
        import signal
        import threading

        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        started = time.monotonic()
        run_worker(stop=stop)
        print(json.dumps({"measure_extract": {"state": "stopped", "seconds": round(time.monotonic() - started)}}))


if __name__ == "__main__":
    main()
