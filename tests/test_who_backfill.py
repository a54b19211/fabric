"""The bounded WHO backfill (flows.who_backfill): every bound, the ceiling, the receipt. The WHO endpoint and the
history store are stand-ins; nothing goes to the network or a database."""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from test_source_refresh import who_value

from biz_rules import who_don
from collection import rate
from flows.who_backfill import SINCE, Counts, backfill_who

UNTIL = datetime(2026, 11, 9, tzinfo=timezone.utc)
FIRST_NEW = datetime(2024, 1, 2, tzinfo=timezone.utc)
REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    """The 30 s spacing of the who.int gate is not under test here; the counters of the ceiling are."""
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
    """The calls of db.history that the backfill makes, over a dict of runs; finish_fetch keeps the coverage the way
    db.history does (the real one is checked in tests/integration_who_backfill.py)."""

    def __init__(self):
        self.runs = {}

    def create_fetch(self, request, key):
        if key in self.runs:
            assert self.runs[key].request == request  # db.history: the same key with another request is a conflict
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


def test_backfill_stops_when_its_request_bound_is_reached(monkeypatch):
    who, store = WhoStandIn(monkeypatch), RunStore()
    receipt = backfill_who(UNTIL, bounds=Counts(records=400, bytes=40 << 20, requests=3), store=store)
    assert receipt["stopped_by"] == "requests_bound" and len(who.requests) == 3  # the 4th is not even sent
    assert receipt["spent"]["requests"] == 3 and receipt["spent"]["records"] == 150


def test_backfill_stops_when_its_byte_bound_is_reached_and_does_not_keep_the_page_that_crossed_it(monkeypatch):
    who, store = WhoStandIn(monkeypatch, pad=1500), RunStore()
    page = len(json.dumps({"value": who.notices[20:71]}))  # a response of the first page, give or take the spacing
    receipt = backfill_who(UNTIL, bounds=Counts(records=400, bytes=page * 3 // 2, requests=20), store=store)
    assert receipt["stopped_by"] == "bytes_bound" and len(who.requests) == 2  # the first page fits, the second crosses
    assert receipt["spent"]["bytes"] > page * 3 // 2 and receipt["spent"]["records"] == 50
    assert receipt["cursor"]["id"] == adopted(store)[-1]  # the second page is the gap: it starts at the cursor


def test_backfill_leg_fails_on_one_response_over_8_mib_and_adopts_nothing_from_it(monkeypatch):
    who, store = WhoStandIn(monkeypatch, count=30), RunStore()
    who.notices[20]["Summary"] = "<p>" + "x" * (9 << 20) + "</p>"  # the first notice since 2024 is 9 MiB
    receipt = backfill_who(UNTIL, store=store)
    assert receipt["stopped_by"] == "source_failed" and receipt["spent"]["records"] == 0 and adopted(store) == []
    assert len(who.requests) == 1  # a failed leg is the end of this call, not a retry loop


def test_backfill_requests_count_inside_the_daily_ceiling_and_a_later_call_carries_on(monkeypatch, mid_hour_clock):
    who, store = WhoStandIn(monkeypatch), RunStore()
    for _ in range(197):  # the scheduled watch has used 197 of the day's 200 requests
        assert rate.take_for_url(who_don.ENDPOINT) is None
    stopped = backfill_who(UNTIL, store=store)
    assert stopped["stopped_by"] == "outbound_quota_day" and len(who.requests) == 3  # the 4th was refused, not sent
    assert stopped["spent"]["records"] == 150 and not stopped["reached_end"]
    tomorrow = SimpleNamespace(time=lambda: 1_800_000_000 + 86400.0, monotonic=time.monotonic, sleep=time.sleep)
    monkeypatch.setattr(rate, "time", tomorrow)  # the ceiling counts per UTC day
    done = backfill_who(UNTIL, store=store)
    assert done["stopped_by"] == "records_bound" and done["spent"]["records"] == 400 and done["spent"]["requests"] == 8
    assert len(who.requests) == 8 and len(adopted(store)) == 400  # the legs already made were replayed, not read again
