"""Explicit, bounded backfill of WHO Disease Outbreak News (task book 01, 2.1).

Run by hand, once, before the freeze:  python -m flows.who_backfill --until 2026-11-09T00:00:00Z
A recent window is another task:           python -m flows.who_backfill --since 2026-10-08T00:00:00Z --until ...

It reads the notices with LastModified from 2024-01-01 (or --since) up to the frozen --until, oldest first, each in its current
version, and stops at the first bound it reaches: 400 records, 40 MiB, 20 requests. The requests count against the
200 a day this instance may send to who.int (collection.rate), together with the scheduled watch's. The plugin takes
50 records from a response and a history run reads at most 100, so the backfill goes in legs of one run each.

The coverage of every leg carries a receipt under "backfill": the bounds, what has been spent, why the backfill
stopped (None while it goes on), the LastModified/Id cursor the next leg would start from, and the versions this run
created. That receipt is what marks the read as background: those versions were first seen by the backfill, so they
are neither new activity nor part of an activity baseline. A version an earlier run already adopted is not listed.

The legs have fixed idempotency keys. A second call with the same --until replays the legs already made without
sending anything and carries on after them, so the bounds hold across calls, a call after a ceiling stop continues
where the last one stopped, and a finished backfill answers with its last receipt. A different --since is a different
task: its legs carry the start in their keys, so the earlier task and its receipts are left as they are.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from biz_rules import who_don
from biz_rules.history_models import FetchRequest, HistoryError
from biz_rules.history_sources import read_public
from collection import rate, transport
from db import history

LOG = logging.getLogger(__name__)
SINCE = datetime(2024, 1, 1, tzinfo=timezone.utc)
LEG_PAGES, LEG_DEADLINE = 2, 120
LEG_RECORDS = LEG_PAGES * who_don.PAGE_SIZE  # a history run reads at most 100 records
# Stops that end the backfill; any other stop (a ceiling, a failure, a deadline) ends one call and a later call goes on.
FINAL = frozenset({"reached_end", "records_bound", "bytes_bound", "requests_bound"})


@dataclass(frozen=True)
class Counts:
    records: int = 0
    bytes: int = 0
    requests: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(self.records + other.records, self.bytes + other.bytes, self.requests + other.requests)

    def reached(self, bounds: Counts) -> str | None:
        """The first bound these counts have reached."""
        return next((f"{name}_bound" for name in asdict(self) if getattr(self, name) >= getattr(bounds, name)), None)


BOUNDS = Counts(records=400, bytes=40 * 1024 * 1024, requests=20)


@dataclass
class Progress:
    """What the legs made so far add up to."""

    legs: int = 0
    spent: Counts = Counts()
    continuation: dict | None = None  # where the next leg starts, as the plugin handed it back


def _key(since: datetime, until: datetime, leg: int) -> str:
    window = f"{until:%Y%m%dT%H%M%SZ}" if since == SINCE else f"{since:%Y%m%dT%H%M%SZ}-{until:%Y%m%dT%H%M%SZ}"
    return f"who-backfill:{window}:{leg}"


def _leg_request(since: datetime, until: datetime, progress: Progress, bounds: Counts) -> FetchRequest:
    return FetchRequest(
        source_id="who_don", query="*", updated_since=since, updated_until=until,
        max_records=min(LEG_RECORDS, bounds.records - progress.spent.records), max_pages=LEG_PAGES,
        deadline_seconds=LEG_DEADLINE, continuation=progress.continuation,
    )


def _stopped_by(batch: dict, spent: Counts, bounds: Counts) -> str | None:
    """Why the backfill cannot go on after this leg, or None when it can."""
    if batch["upstream_pagination_exhausted"]:
        return "reached_end"
    if bound := spent.reached(bounds):
        return bound
    if batch["error"]:
        return "source_failed"
    if batch["stop_reason"] in rate.CEILING_STOPS or not batch["records"] or batch["next_continuation"] is None:
        return batch["stop_reason"]
    return None


def _read_leg(store, run, bounds: Counts, progress: Progress) -> dict:
    """Read one leg inside what the bounds leave: the budget of this one read is the rest of the backfill's."""
    request, _ = store.job_input(run.id, run.attempt)
    budget = transport.Budget.for_request(request)
    budget.max_requests = min(budget.max_requests, bounds.requests - progress.spent.requests)
    budget.max_bytes = min(budget.max_bytes, bounds.bytes - progress.spent.bytes)
    try:
        with transport.metered(budget):
            return read_public(request)
    except HistoryError as exc:
        store.fail_job(run.id, run.attempt, exc.code, exc.message)
        raise


def _run_leg(store, run, since: datetime, until: datetime, bounds: Counts, progress: Progress):
    batch = _read_leg(store, run, bounds, progress)
    spent = progress.spent + Counts(len(batch["records"]), batch["bytes"], batch["requests"])
    stopped_by = _stopped_by(batch, spent, bounds)
    ahead = batch["next_continuation"] or progress.continuation
    receipt = {
        "background": True, "leg": progress.legs + 1, "since": since.isoformat(), "until": until.isoformat(),
        "bounds": asdict(bounds), "spent": asdict(spent), "stopped_by": stopped_by,
        "reached_end": stopped_by == "reached_end",
        "cursor": None if stopped_by == "reached_end" or not ahead else json.loads(ahead["cursor"]),
        "note": None if stopped_by == "reached_end" else "回填有界，未到底",
    }
    LOG.info("who_don backfill leg %s: stopped_by=%s spent=%s", receipt["leg"], stopped_by, receipt["spent"])
    return store.finish_fetch(run.id, run.attempt, {**batch, "backfill": receipt})


def _absorb(progress: Progress, run) -> dict | None:
    """Count a leg that is finished: its receipt (None for a leg that failed before it could leave one)."""
    receipt = run.coverage.get("backfill")
    progress.legs += 1
    if receipt:
        progress.spent = Counts(**receipt["spent"])
        progress.continuation = run.coverage.get("next_continuation") or progress.continuation
    return receipt


def backfill_who(until: datetime, *, since: datetime = SINCE, bounds: Counts = BOUNDS, store=history) -> dict:
    """Run, or carry on, the backfill of [since, until]; the receipt of the last leg it came to."""
    since, until = since.astimezone(timezone.utc), until.astimezone(timezone.utc)
    if until <= since:
        raise HistoryError("backfill_window", "回填终点须晚于起点（默认 2024-01-01）")
    progress = Progress()
    while True:
        run, _ = store.create_fetch(_leg_request(since, until, progress, bounds), _key(since, until, progress.legs + 1))
        if run.state in ("completed", "partial", "failed"):  # made by an earlier call
            receipt, fresh = _absorb(progress, run), False
        elif run.state == "queued":
            receipt, fresh = _absorb(progress, _run_leg(store, run, since, until, bounds, progress)), True
        else:
            raise HistoryError("backfill_leg_unresolved", "回填的上一段仍在运行或待恢复；先按任务编号处理它", 409)
        if receipt is None:  # a leg that failed before it could leave a receipt: nothing was spent, go on
            continue
        if receipt["stopped_by"] in FINAL or (fresh and receipt["stopped_by"]):
            return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded backfill of WHO Disease Outbreak News (task book 01, 2.1)")
    parser.add_argument("--until", required=True, help="the freeze moment, ISO 8601 with a time zone")
    parser.add_argument("--since", help="LastModified start, ISO 8601 with a time zone (default 2024-01-01)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    def moment(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    receipt = backfill_who(moment(args.until), since=moment(args.since) if args.since else SINCE)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
