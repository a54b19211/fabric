"""The measurement worker of the processing layer: per round, plan runs for new clean artifacts, read due windows
within the round's model request budget (a spent budget ends the round; the windows wait as budget_spent), resume
runs whose source is readable again and follow the corrections sources state. The BioCheck checks of the readings
are the extraction-check worker's (db.extraction_checks, producer "measures").

    python -m flows.measure_extract            # rounds until SIGTERM; disabled without DATAFABRIC_MEASURE_LLM_*
"""

from __future__ import annotations

import json
import os
import time

from sqlalchemy import text

from db import history, measure_runs, measurements

ROUND_REQUESTS = int(os.environ.get("DATAFABRIC_MEASURE_ROUND_REQUESTS", "50"))
ROUND_WINDOWS = 200


def _sources(conn):
    return [r[0] for r in conn.execute(text("SELECT source_id FROM history_sources WHERE read_enabled ORDER BY source_id"))
            if measure_runs._readable(conn, r[0])]


def run_round(*, budget_requests=ROUND_REQUESTS, max_windows=ROUND_WINDOWS, model=None, client=None) -> dict:
    model = model or measure_runs.model_from_env()
    if model is None:
        return {"state": "disabled"}
    counts = {"planned": measure_runs.plan(model=model), "resumed": measure_runs.resume(), "windows": {},
              "budget_spent": False}
    budget = measure_runs.RequestBudget(budget_requests)
    for _ in range(max_windows):
        if budget.spent():
            counts["budget_spent"] = True
            break
        state = measure_runs.process_once(model=model, client=client, budget=budget)["state"]
        counts["windows"][state] = counts["windows"].get(state, 0) + 1
        if state == "idle":
            break
    with history.history_engine().begin() as conn:
        counts["corrected_versions"] = len(measurements.scan_corrections(conn, _sources(conn)))
    return counts


def run_worker(*, stop, idle_seconds=30.0, rounds=None):
    n = 0
    while not stop.is_set() and (rounds is None or n < rounds):
        n += 1
        counts = run_round()
        if counts.get("state") == "disabled" or counts["windows"].get("idle"):
            stop.wait(idle_seconds)


if __name__ == "__main__":
    import signal
    import threading

    if measure_runs.model_from_env() is None:
        print(json.dumps({"measure_extract": {"state": "disabled"}}))
    else:
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        started = time.monotonic()
        run_worker(stop=stop)
        print(json.dumps({"measure_extract": {"state": "stopped", "seconds": round(time.monotonic() - started)}}))
