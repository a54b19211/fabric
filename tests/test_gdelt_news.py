"""新闻索引故障注入，不下载新闻正文或假冒独立来源。"""
import json

import httpx
import pytest

from biz_rules.gdelt_news import article_id
from biz_rules.history_models import ArchiveRecord, FetchRequest
from biz_rules.history_sources import project_record, read_public
from collection import rate
from config.settings import APP

HOUR = {"updated_since": "2026-10-30T00:00:00Z", "updated_until": "2026-10-30T01:00:00Z"}


def article(**kw):
    return {"url": "https://example.org/article?id=1", "title": "SYNTHETIC report",
            "seendate": "20261030T010000Z", "domain": "example.org", **kw}


def client(monkeypatch, handler):
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "true")  # Only this mocked transport may fetch.
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.fixture
def open_gate(monkeypatch):
    """The 5 s spacing of the shared rate gate is not under test here; the windows are."""
    monkeypatch.setattr(rate, "wait_for_url", lambda url, **kw: None)


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
def test_gdelt_failure_never_becomes_zero_news(monkeypatch, failure):
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
    assert result["stop_reason"] == "source_failed" and result["requests"] == 1
    if failure == "conflict":
        assert result["records"] == []


def test_gdelt_confirmed_empty_response_is_distinct_from_failure(monkeypatch):
    client(monkeypatch, lambda request: httpx.Response(200, json={"articles": []}))
    result = read_public(FetchRequest(source_id="gdelt_doc", query="no match"))
    assert result["records"] == [] and result["error"] is None
    assert result["stop_reason"] == "bounded_index_response" and result["complete"] is False
    [receipt] = result["window_coverage"]  # a read without a window is one window that cannot be cut
    assert (receipt["status"], receipt["returned"], receipt["start"]) == ("read", 0, None)


def test_gdelt_rate_limit_after_retries_is_a_budget_stop_not_zero_news(monkeypatch):
    """The shared transport retries 429 and timeouts; when the retry wait no longer fits the deadline the read stops
    at the budget. That is reported as the stop reason, never as an empty successful response."""
    monkeypatch.setattr(APP, "TASK_RETRIES", 2)
    monkeypatch.setattr(APP, "TASK_RETRY_DELAY", 60)
    client(monkeypatch, lambda request: httpx.Response(429))
    result = read_public(FetchRequest(source_id="gdelt_doc", query="pneumonia", deadline_seconds=5))
    assert result["records"] == [] and result["error"] is None
    assert result["stop_reason"] == "deadline" and not result["upstream_pagination_exhausted"]


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


def test_failed_window_is_not_zero_news_and_the_rest_stays_unread(monkeypatch, open_gate):
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


def test_request_budget_stop_reports_the_windows_not_reached(monkeypatch, open_gate):
    sent = stand_in(monkeypatch, lambda start, end: 3)
    result = read_public(FetchRequest(source_id="gdelt_doc", query="outbreak", max_records=3, max_pages=1,
                                      updated_since="2026-10-30T00:00:00Z", updated_until="2026-10-30T02:00:00Z"))
    assert len(sent) == 10 and result["requests"] == 10  # max_pages=1 allows 10 requests; two full hours need 14
    assert result["stop_reason"] == "request_budget" and result["upstream_pagination_exhausted"] is False
    unread = [w for w in result["window_coverage"] if w["status"] == "not_read"]
    assert [(w["start"][11:16], w["end"][11:16], w["reason"]) for w in unread] == [
        ("01:15", "01:30", "request_budget"), ("01:30", "02:00", "request_budget")]
