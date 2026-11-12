"""新闻索引故障注入，不下载新闻正文或假冒独立来源。"""
import json
from datetime import datetime, timezone

import httpx
import pytest

from biz_rules import gdelt_news, window_coverage
from biz_rules.gdelt_news import ENDPOINT, article_id
from biz_rules.history_models import ArchiveRecord, Coverage, FetchRequest, HistoryError
from biz_rules.history_sources import project_record, read_public
from biz_rules.window_coverage import effective_receipts
from collection import rate, transport
from collection.windows import cycle_request
from config.settings import APP
from db.history import _lead_window, _list_coverage
from db.source_refresh import SourceWatch, _page_finished

HOUR = {"updated_since": "2026-10-30T00:00:00Z", "updated_until": "2026-10-30T01:00:00Z"}
CYCLE_NOW = datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)  # a watch cycle reads the whole hours before 12:00


def article(**kw):
    return {"url": "https://example.org/article?id=1", "title": "SYNTHETIC report",
            "seendate": "20261030T010000Z", "domain": "example.org", **kw}


def client(monkeypatch, handler):
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "true")  # Only this mocked transport may fetch.
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.fixture
def open_gate(monkeypatch):
    """The 6 s spacing of the shared rate gate is not under test here; the windows are."""
    monkeypatch.setattr(rate, "wait_for_url", lambda url, **kw: None)


@pytest.fixture
def waits(monkeypatch, open_gate):
    """The seconds a read sleeps (a 429 is answered after a wait); nothing really sleeps."""
    slept = []
    monkeypatch.setattr(gdelt_news.time, "sleep", slept.append)
    return slept


def stand_in(monkeypatch, count_for):
    """A GDELT stand-in answering each window with count_for(start, end) articles, every one with its own url.
    Returns the (startdatetime, enddatetime) of each request, in the order sent."""
    sent = []

    def respond(request):
        window = request.url.params["startdatetime"], request.url.params["enddatetime"]
        sent.append(window)
        return httpx.Response(200, json={"articles": [
            article(url=f"https://example.org/{window[0]}/{window[1]}/{n}") for n in range(count_for(*window))]})
    client(monkeypatch, respond)
    return sent


def test_news_metadata_preserves_url_identity_and_does_not_invent_fulltext():
    value = article()
    item = ArchiveRecord(source_record_id=article_id(value), record_kind="document", raw_content=json.dumps(value))
    record = project_record("gdelt_doc", item)
    assert record["document_evidence"]["material_kind"] == "news_report"
    assert record["document_evidence"]["text_level"] == "title"
    assert record["document_evidence"]["source_independence"]["status"] == "unknown"
    assert record["source_published_at"] is None and record["event_time"] is None
    assert record["source_updated_at"]["meaning"] == "gdelt_first_observed"
    assert article_id(article(url="https://example.org/article?id=2")) != item.source_record_id


def test_gdelt_raw_metadata_bounded_replay_and_explicit_window(monkeypatch, open_gate):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"articles": [article(), article()]})
    client(monkeypatch, respond)
    query = FetchRequest(source_id="gdelt_doc", query="pneumonia", max_records=3,
        updated_since="2026-10-29T08:00:00+08:00", updated_until="2026-10-29T09:00:00+08:00")
    result = read_public(query)
    assert result["error"] is None and len(result["records"]) == 1
    assert not result["complete"] and result["next_continuation"] is None
    assert result["upstream_pagination_exhausted"]  # the window stayed below the cap; complete stays False above
    assert len(seen) == 1 and seen[0].url.host == "api.gdeltproject.org"
    assert seen[0].url.params["startdatetime"] == "20261029000000"
    assert seen[0].url.params["enddatetime"] == "20261029010000"


@pytest.mark.parametrize("failure", ["rate", "timeout", "shape", "conflict", "bad_json"])
def test_gdelt_failure_never_becomes_zero_news(monkeypatch, waits, failure):
    monkeypatch.setattr(APP, "TASK_RETRIES", 0)  # one physical request; retries are covered below
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if failure == "rate":
            return httpx.Response(429)
        if failure == "shape":
            return httpx.Response(200, json={})
        if failure == "bad_json":
            return httpx.Response(200, text="upstream unavailable")
        return httpx.Response(200, json={"articles": [article(), article(title="changed in same response")]})
    client(monkeypatch, respond)
    result = read_public(FetchRequest(source_id="gdelt_doc", query="pneumonia"))
    assert result["error"] and not result["upstream_pagination_exhausted"]
    assert result["stop_reason"] == "source_failed" and result["requests"] == (2 if failure == "rate" else 1)  # a 429 is asked twice
    if failure == "conflict":
        assert result["records"] == []


def test_gdelt_confirmed_empty_response_is_distinct_from_failure(monkeypatch):
    client(monkeypatch, lambda request: httpx.Response(200, json={"articles": []}))
    result = read_public(FetchRequest(source_id="gdelt_doc", query="no match"))
    assert result["records"] == [] and result["error"] is None
    assert result["stop_reason"] == "bounded_index_response" and result["complete"] is False
    [receipt] = result["window_coverage"]  # a read without a window is one window that cannot be cut
    assert (receipt["status"], receipt["returned"], receipt["start"]) == ("read", 0, None)


LIMITED = "Please limit requests to one every 5 seconds or contact kalev.leetaru5@gmail.com for larger queries."


def test_gdelt_429_waits_the_interval_it_names_and_asks_once_more(monkeypatch, waits):
    """GDELT asks for one request every 5 seconds. A 429 is answered once: wait that interval (plus a second of
    margin) and send the request again; the second answer is read as usual."""
    answers = [httpx.Response(429, text=LIMITED), httpx.Response(200, json={"articles": [article()]})]
    seen = []
    client(monkeypatch, lambda request: seen.append(request) or answers.pop(0))
    result = read_public(FetchRequest(source_id="gdelt_doc", query="pneumonia"))
    assert result["error"] is None and len(result["records"]) == 1
    assert len(seen) == 2 and result["requests"] == 2 and waits == [6]


def test_gdelt_second_429_ends_the_read_and_says_the_other_side_limited_us(monkeypatch, waits):
    """After the one wait and resend a second 429 stops the read: a failed read whose reason says "对方限流",
    never an empty successful response."""
    seen = []
    client(monkeypatch, lambda request: seen.append(request) or httpx.Response(429, text=LIMITED))
    result = read_public(FetchRequest(source_id="gdelt_doc", query="pneumonia"))
    assert len(seen) == 2 and result["requests"] == 2 and result["records"] == [] and waits == [6]
    assert result["stop_reason"] == "source_failed" and not result["upstream_pagination_exhausted"]
    assert result["error"]["code"] == "source_rate_limited"
    assert "对方限流" in result["error"]["message"] and "仍是 429" in result["error"]["message"]


def test_gdelt_429_with_no_time_left_to_wait_says_so_and_does_not_resend(monkeypatch, waits):
    """The shortest deadline (5 s) cannot hold a 6 s wait: no second request, and the reason names the deadline."""
    seen = []
    client(monkeypatch, lambda request: seen.append(request) or httpx.Response(429, text=LIMITED))
    result = read_public(FetchRequest(source_id="gdelt_doc", query="pneumonia", deadline_seconds=5))
    assert len(seen) == 1 and result["requests"] == 1 and result["records"] == [] and waits == []
    assert result["stop_reason"] == "source_failed"
    assert result["error"]["code"] == "source_rate_limited" and "期限内等不到重试" in result["error"]["message"]


def test_full_windows_are_halved_to_15_minutes_and_the_finest_ones_are_marked_truncated(monkeypatch, open_gate):
    sent = stand_in(monkeypatch, lambda start, end: 3)  # every response reaches the cap of 3
    result = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, **HOUR))
    assert sent == [
        ("20261030000000", "20261030010000"),
        ("20261030000000", "20261030003000"), ("20261030000000", "20261030001500"), ("20261030001500", "20261030003000"),
        ("20261030003000", "20261030010000"), ("20261030003000", "20261030004500"), ("20261030004500", "20261030010000"),
    ]  # 1 + 2 + 4 requests: the most one query-hour may use
    windows = result["window_coverage"]
    assert [w["status"] for w in windows] == ["split", "split", "read", "read", "split", "read", "read"]
    assert [w["possibly_truncated"] for w in windows] == [False, False, True, True, False, True, True]
    assert all(w["returned"] == 3 and w["full"] and w["requests"] == 1 for w in windows)
    marks = ["00:00", "00:15", "00:30", "00:45", "01:00"]
    assert result["truncated_intervals"] == [
        {"start": f"2026-10-30T{a}:00+00:00", "end": f"2026-10-30T{b}:00+00:00"} for a, b in zip(marks, marks[1:])]
    assert result["requests"] == 7 and len(result["records"]) == 21
    assert result["upstream_pagination_exhausted"] is False and result["complete"] is False
    assert result["stop_reason"] == "bounded_index_response"


def test_halving_stops_where_a_window_comes_back_below_the_cap(monkeypatch, open_gate):
    full = {("20261030000000", "20261030010000"), ("20261030000000", "20261030003000")}
    sent = stand_in(monkeypatch, lambda start, end: 3 if (start, end) in full else 1)
    result = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, **HOUR))
    assert len(sent) == 5  # the hour, its first half, that half's two quarters, the second half
    windows = result["window_coverage"]
    assert [(w["status"], w["returned"], w["full"]) for w in windows] == [
        ("split", 3, True), ("split", 3, True), ("read", 1, False), ("read", 1, False), ("read", 1, False)]
    assert result["truncated_intervals"] == [] and result["upstream_pagination_exhausted"] is True


def test_window_below_the_cap_ends_after_one_request_without_claiming_the_whole_window(monkeypatch, open_gate):
    sent = stand_in(monkeypatch, lambda start, end: 2)
    result = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, **HOUR))
    assert len(sent) == 1
    [receipt] = result["window_coverage"]
    assert (receipt["status"], receipt["returned"], receipt["distinct_urls"]) == ("read", 2, 2)
    assert receipt["full"] is False and receipt["possibly_truncated"] is False
    assert result["complete"] is False and result["source_total"] is None  # below the cap is not "the whole window"


def test_failed_window_is_not_zero_news_and_the_rest_stays_unread(monkeypatch, waits):
    monkeypatch.setattr(APP, "TASK_RETRIES", 0)
    def respond(request):
        if request.url.params["enddatetime"] == "20261030003000":
            return httpx.Response(429)
        return httpx.Response(200, json={"articles": [article(url=f"https://example.org/{n}") for n in range(3)]})
    client(monkeypatch, respond)
    result = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, **HOUR))
    assert result["stop_reason"] == "source_failed" and result["error"]["code"] == "source_rate_limited"
    assert [(w["status"], w["returned"], w["reason"]) for w in result["window_coverage"]] == [
        ("split", 3, None), ("failed", None, "source_rate_limited"), ("not_read", None, "earlier_window_failed")]
    assert len(result["records"]) == 3 and result["upstream_pagination_exhausted"] is False


def test_a_deadline_stop_reports_the_windows_not_reached(monkeypatch, open_gate):
    """A stop in the middle of a read (here the deadline, which falls before the 11th request) lists the windows it did
    not reach; the read has no limit on the number of requests of its own, two full hours need 14."""
    calls = []

    def take(url):  # the ceiling of 7 an hour is not under test here
        calls.append(url)
        if len(calls) == 11:
            transport.current().stop("deadline")
        return None

    monkeypatch.setattr(rate, "take_for_url", take)
    sent = stand_in(monkeypatch, lambda start, end: 3)
    result = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3,
                                      updated_since="2026-10-30T00:00:00Z", updated_until="2026-10-30T02:00:00Z"))
    assert len(sent) == 10 and result["requests"] == 10
    assert result["stop_reason"] == "deadline" and result["upstream_pagination_exhausted"] is False
    unread = [w for w in result["window_coverage"] if w["status"] == "not_read"]
    assert [(w["start"][11:16], w["end"][11:16], w["reason"]) for w in unread] == [
        ("01:15", "01:30", "deadline"), ("01:30", "02:00", "deadline")]


def test_hourly_ceiling_counts_across_reads_per_query_and_names_the_reason(monkeypatch, open_gate, mid_hour_clock):
    stand_in(monkeypatch, lambda start, end: 3)  # every window full: one query-hour uses all 7 requests
    first = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, **HOUR))
    assert first["requests"] == 7 and first["stop_reason"] == "bounded_index_response"
    later = {"updated_since": "2026-10-30T01:00:00Z", "updated_until": "2026-10-30T02:00:00Z"}
    second = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, **later))
    assert second["requests"] == 0 and second["stop_reason"] == "outbound_quota_hour"  # a later read, the same hour
    assert second["window_coverage"][0]["reason"] == "outbound_quota_hour"
    other = read_public(FetchRequest(source_id="gdelt_doc", query="pneumonia", max_records=3, **later))
    assert other["requests"] == 7  # the count is per query


def test_daily_ceiling_stops_the_read_before_its_first_request_and_names_the_reason(
        monkeypatch, open_gate, mid_hour_clock):
    sent = stand_in(monkeypatch, lambda start, end: 1)
    for n in range(900):  # 900 requests of other queries already sent today
        assert rate.take_for_url(f"{ENDPOINT}?query=other{n}") is None
    result = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, **HOUR))
    assert result["stop_reason"] == "outbound_quota_day" and result["requests"] == 0 and sent == []
    [receipt] = result["window_coverage"]
    assert (receipt["status"], receipt["reason"]) == ("not_read", "outbound_quota_day")


def watch_cycle(checkpoint):
    """The state of a GDELT watch at its checkpoint, and the request its cycle sends: the whole hours up to 12:00."""
    watch = SourceWatch.model_validate(
        {"id": "g", "request": {"source_id": "gdelt_doc", "query": "outbreak", "max_records": 3}})
    state = {"window_checkpoint": checkpoint}
    request, _, end = cycle_request(watch, state, CYCLE_NOW)
    state.update(window_ended_at=end.isoformat(), request=request)
    return watch, state, FetchRequest.model_validate(request)


def finish_cycle(watch, state, request):
    batch = read_public(request)
    _page_finished(watch, state, {key: value for key, value in batch.items() if key != "records"}, CYCLE_NOW)
    return batch


def test_cycle_with_windows_cut_short_ends_with_the_gaps_and_moves_the_checkpoint(monkeypatch, open_gate):
    stand_in(monkeypatch, lambda start, end: 3)  # every window stays full down to 15 minutes
    watch, state, request = watch_cycle("2026-11-04T11:00:00+00:00")
    finish_cycle(watch, state, request)
    assert state["state"] == "idle" and state["last_success_at"] and "request" not in state
    assert state["window_checkpoint"] == "2026-11-04T12:00:00+00:00"  # past the cut-short hour: it is not retried
    assert len(state["last_coverage"]["truncated_intervals"]) == 4  # and the gaps stay in the receipt


def test_cycle_stopped_by_a_ceiling_resumes_at_the_first_window_it_did_not_read(monkeypatch, open_gate, mid_hour_clock):
    stand_in(monkeypatch, lambda start, end: 3)  # the first hour takes this query's 7 requests of the hour
    watch, state, request = watch_cycle("2026-11-04T10:00:00+00:00")
    batch = finish_cycle(watch, state, request)
    assert batch["stop_reason"] == "outbound_quota_hour"
    assert state["state"] == "idle" and "last_success_at" not in state  # not a finished cycle
    assert state["window_checkpoint"] == "2026-11-04T11:00:00+00:00"
    unread = [w for w in state["last_coverage"]["window_coverage"] if w["status"] == "not_read"]
    assert [(w["start"][11:16], w["reason"]) for w in unread] == [("11:00", "outbound_quota_hour")]


# ---- the lead channel: the receipts a listing of a window carries ----
def utc(clock):
    return datetime.fromisoformat("2026-10-30T" + clock + ":00+00:00")


def hour_with_a_full_first_half(monkeypatch):
    """One read of 00:00-01:00 whose first half is full at every size, down to its two 15-minute windows, and whose
    second half comes back below the cap. Returns the run as the (query, coverage) pair a listing reads."""
    stand_in(monkeypatch, lambda start, end: 3 if start < "20261030003000" else 1)
    result = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, **HOUR))
    return "outbreak", result


def test_receipts_of_a_window_name_the_windows_that_may_be_cut_short(monkeypatch, open_gate):
    run = hour_with_a_full_first_half(monkeypatch)
    receipts, truncated = effective_receipts([run], (utc("00:00"), utc("01:00")))
    assert [(r.start[11:16], r.end[11:16], r.status, r.possibly_truncated) for r in receipts] == [
        ("00:00", "01:00", "split", False), ("00:00", "00:30", "split", False),
        ("00:00", "00:15", "read", True), ("00:15", "00:30", "read", True), ("00:30", "01:00", "read", False)]
    assert [(i.start[11:16], i.end[11:16]) for i in truncated] == [("00:00", "00:15"), ("00:15", "00:30")]
    receipts, truncated = effective_receipts([run], (utc("00:30"), utc("01:00")))  # the clean half: no truncation to report
    assert [(r.start[11:16], r.end[11:16]) for r in receipts] == [("00:00", "01:00"), ("00:30", "01:00")]
    assert truncated == []
    assert effective_receipts([run], (utc("02:00"), utc("03:00"))) == ([], [])


def test_a_later_run_replaces_the_receipt_of_a_window_an_earlier_one_did_not_reach():
    def receipt(start, end, status, **fields):
        return {"start": "2026-10-30T" + start + ":00+00:00", "end": "2026-10-30T" + end + ":00+00:00",
                "status": status, **fields}
    early = {"window_coverage": [receipt("00:00", "00:30", "read", returned=1),
                                 receipt("00:30", "01:00", "not_read", reason="outbound_quota_hour")]}
    late = {"window_coverage": [receipt("00:30", "01:00", "read", returned=2)]}
    other_query = {"window_coverage": [receipt("00:30", "01:00", "failed", reason="source_rate_limited")]}
    receipts, _ = effective_receipts([("a", early), ("a", late), ("b", other_query)])
    assert [(r.start[11:16], r.status) for r in receipts] == [("00:00", "read"), ("00:30", "read"), ("00:30", "failed")]


def test_a_window_with_too_many_receipts_is_refused_not_cut(monkeypatch, open_gate):
    run = hour_with_a_full_first_half(monkeypatch)
    monkeypatch.setattr(window_coverage, "MAX_RECEIPTS", 4)
    with pytest.raises(HistoryError) as refused:
        effective_receipts([run])
    assert refused.value.code == "window_too_wide"


def test_the_listing_coverage_carries_the_receipts_and_never_claims_the_whole_source(monkeypatch, open_gate):
    receipts = effective_receipts([hour_with_a_full_first_half(monkeypatch)], (utc("00:00"), utc("01:00")))
    coverage = Coverage(**_list_coverage(None, {}, 9, 12, datetime.now(timezone.utc), False, True, receipts))
    assert [i.start[11:16] for i in coverage.truncated_intervals] == ["00:00", "00:15"]
    assert sum(r.possibly_truncated for r in coverage.window_coverage) == 2
    assert coverage.complete is False and "possibly_truncated" in " ".join(coverage.limitations)
    plain = Coverage(**_list_coverage(None, {}, 9, 12, datetime.now(timezone.utc), False, False, ([], [])))
    assert plain.window_coverage == [] and plain.truncated_intervals == []


def test_a_lead_window_needs_both_bounds_with_a_time_zone_and_a_source_that_has_windows():
    assert _lead_window("gdelt_doc", (None, None)) is None
    assert _lead_window("gdelt_doc", (utc("00:00"), utc("01:00"))) == (utc("00:00"), utc("01:00"))
    for source, window in (("gdelt_doc", (utc("00:00"), None)), ("gdelt_doc", (utc("01:00"), utc("00:00"))),
                           ("gdelt_doc", (datetime(2026, 10, 30), utc("01:00"))),
                           ("clinicaltrials", (utc("00:00"), utc("01:00")))):
        with pytest.raises(HistoryError) as refused:
            _lead_window(source, window)
        assert refused.value.code == "invalid_window"
