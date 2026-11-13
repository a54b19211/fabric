"""The bounded WHO backfill (ops.who_backfill): every bound, the receipt. The WHO endpoint and the
history store are stand-ins; nothing goes to the network or a database."""

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from test_source_refresh import who_value

from fetch import rate, who_don
from ops.who_backfill import SINCE, Counts, backfill_who
from store.history_models import HistoryError

UNTIL = datetime(2026, 11, 9, tzinfo=timezone.utc)
FIRST_NEW = datetime(2024, 1, 2, tzinfo=timezone.utc)
REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    """The 30 s spacing of the who.int gate is not under test here."""
    monkeypatch.setattr(rate, "wait_for_url", lambda url, **kw: None)


def notice(n, pad=0):
    """Notice n: notices 1-20 date from 2023, the rest from 2024-01-02, one hour apart."""
    modified = datetime(2023, 12, 1, tzinfo=timezone.utc) + timedelta(hours=n) if n <= 20 else FIRST_NEW + timedelta(hours=n)
    return {**who_value(n), "LastModified": modified.strftime("%Y-%m-%dT%H:%M:%SZ"), "Summary": "<p>" + "x" * pad + "</p>"}


class WhoStandIn:
    """The WHO OData endpoint over `count` notices, oldest LastModified first; the bounds and the keyset the plugin
    sends are honoured, and every request is kept."""

    def __init__(self, monkeypatch, count=600, pad=0):
        self.notices, self.requests = [notice(n, pad) for n in range(1, count + 1)], []
        monkeypatch.setattr(httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(self), **kw))

    def __call__(self, request):
        self.requests.append(request)
        flt = request.url.params.get("$filter", "")
        low = re.search(r"LastModified ge (\S+)", flt)
        high = re.search(r"LastModified le (\S+)", flt)
        after = re.search(r"LastModified gt (\S+) or", flt)

        def at(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        rows = [
            n for n in self.notices
            if (not low or at(n["LastModified"]) >= at(low[1])) and (not high or at(n["LastModified"]) <= at(high[1]))
            and (not after or at(n["LastModified"]) > at(after[1]))
        ]
        return httpx.Response(200, json={"value": rows[: int(request.url.params["$top"])]})


class RunStore:
    """The calls of store.history that the backfill makes, over a dict of runs; finish_fetch keeps the coverage the way
    store.history does (the real one is checked in tests/integration_who_backfill.py)."""

    def __init__(self):
        self.runs = {}

    def create_fetch(self, request, key):
        if key in self.runs:
            assert self.runs[key].request == request  # store.history: the same key with another request is a conflict
            return self.runs[key], False
        self.runs[key] = SimpleNamespace(id=key, attempt=1, state="queued", coverage={}, request=request)
        return self.runs[key], True

    def job_input(self, run_id, attempt):
        self.runs[run_id].state = "running"
        return self.runs[run_id].request, None

    def finish_fetch(self, run_id, attempt, batch):
        run = self.runs[run_id]
        run.state = "completed"
        run.coverage = {key: batch[key] for key in ("requests", "bytes", "stop_reason")}
        run.coverage["adopted_count"] = len(batch["records"])
        if batch["next_continuation"] is not None:
            run.coverage["next_continuation"] = batch["next_continuation"]
        run.coverage["backfill"] = {**batch["backfill"], "background_version_ids": [r.source_record_id for r in batch["records"]]}
        return run

    def fail_job(self, run_id, attempt, code, message):
        self.runs[run_id].state = "failed"


def adopted(store):
    return [i for run in store.runs.values() for i in run.coverage["backfill"]["background_version_ids"]]


def test_backfill_starts_at_2024_reads_in_legs_and_stops_at_400_records_leaving_its_cursor(monkeypatch):
    who, store = WhoStandIn(monkeypatch), RunStore()
    receipt = backfill_who(UNTIL, store=store)
    assert (receipt["stopped_by"], receipt["reached_end"], receipt["note"]) == ("records_bound", False, "回填有界，未到底")
    assert receipt["spent"]["records"] == 400 and len(store.runs) == 4 and len(who.requests) == 8
    expected = [n["Id"] for n in who.notices if n["LastModified"] >= "2024"][:400]
    assert adopted(store) == expected  # oldest first from 2024-01-01: not one of the 2023 notices
    assert receipt["cursor"]["id"] == expected[-1]  # the LastModified/Id the next leg would start from
    for request in who.requests:
        flt = request.url.params["$filter"]
        assert f"LastModified ge {SINCE.isoformat()}" in flt and f"LastModified le {UNTIL.isoformat()}" in flt
        assert int(request.url.params["$top"]) <= who_don.PAGE_SIZE + 1  # at most 50 records a page (+1 to see the end)
    assert all(run.coverage["backfill"]["background"] for run in store.runs.values())
    assert backfill_who(UNTIL, store=store) == receipt and len(who.requests) == 8  # a finished backfill sends nothing


def test_backfill_with_a_since_is_another_task_that_reads_only_its_window_and_leaves_the_first_alone(monkeypatch):
    who, store = WhoStandIn(monkeypatch, count=200), RunStore()
    first = backfill_who(UNTIL, bounds=Counts(records=100, bytes=40 << 20), store=store)
    sent = len(who.requests)
    since = FIRST_NEW + timedelta(hours=150)
    inside = [n["Id"] for n in who.notices if n["LastModified"] >= since.strftime("%Y-%m-%dT%H:%M:%SZ")]
    window = backfill_who(UNTIL, since=since, store=store)
    assert window["since"] == since.isoformat() and window["reached_end"] and window["spent"]["records"] == len(inside)
    assert len(store.runs) == 2 and first["since"] == SINCE.isoformat()  # its own run; the first task's run is as it was
    assert all(f"LastModified ge {since.isoformat()}" in r.url.params["$filter"] for r in who.requests[sent:])
    assert adopted(store)[-len(inside):] == inside  # only the window's notices, oldest first
    with pytest.raises(HistoryError):
        backfill_who(since, since=since, store=store)  # an end that is not after the start


def test_backfill_stops_after_the_leg_that_reaches_its_byte_bound(monkeypatch):
    who, store = WhoStandIn(monkeypatch, pad=1500), RunStore()
    page = len(json.dumps({"value": who.notices[20:71]}))  # a response of the first page, give or take the spacing
    receipt = backfill_who(UNTIL, bounds=Counts(records=400, bytes=page * 3 // 2), store=store)
    assert receipt["stopped_by"] == "bytes_bound" and len(who.requests) == 2  # the first leg's two pages cross it
    assert receipt["spent"]["bytes"] > page * 3 // 2 and receipt["spent"]["records"] == 100
    assert receipt["cursor"]["id"] == adopted(store)[-1]  # a later backfill would start after the last one kept


def test_backfill_leg_fails_on_one_response_over_8_mib_and_adopts_nothing_from_it(monkeypatch):
    who, store = WhoStandIn(monkeypatch, count=30), RunStore()
    who.notices[20]["Summary"] = "<p>" + "x" * (9 << 20) + "</p>"  # the first notice since 2024 is 9 MiB
    receipt = backfill_who(UNTIL, store=store)
    assert receipt["stopped_by"] == "source_failed" and receipt["spent"]["records"] == 0 and adopted(store) == []
    assert len(who.requests) == 1  # a failed leg is the end of this call, not a retry loop

