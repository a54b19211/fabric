"""新闻索引故障注入，不下载新闻正文或假冒独立来源。"""
import json
import httpx
import pytest
from biz_rules.history_models import ArchiveRecord, FetchRequest
from biz_rules.history_sources import project_record, read_public
from biz_rules.gdelt_news import article_id
from config.settings import APP


def article(**kw):
    return {"url": "https://example.org/article?id=1", "title": "SYNTHETIC report",
            "seendate": "20261030T010000Z", "domain": "example.org", **kw}


def client(monkeypatch, handler):
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "true")  # Only this mocked transport may fetch.
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


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


def test_gdelt_raw_metadata_bounded_replay_and_explicit_window(monkeypatch):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"articles": [article(), article()]})
    client(monkeypatch, respond)
    query = FetchRequest(source_id="gdelt_doc", query="pneumonia", max_records=2,
        updated_since="2026-10-29T08:00:00+08:00", updated_until="2026-10-30T08:00:00+08:00")
    result = read_public(query)
    assert result["error"] is None and len(result["records"]) == 1
    assert not result["complete"] and result["next_continuation"] is None
    assert not result["upstream_pagination_exhausted"]  # bounded results never imply whole-news coverage
    assert len(seen) == 1 and seen[0].url.host == "api.gdeltproject.org"
    assert seen[0].url.params["startdatetime"] == "20261029000000"
    assert seen[0].url.params["enddatetime"] == "20261030000000"


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


def test_gdelt_rate_limit_after_retries_is_a_budget_stop_not_zero_news(monkeypatch):
    """The shared transport retries 429 and timeouts; when the retry wait no longer fits the deadline the read stops
    at the budget. That is reported as the stop reason, never as an empty successful response."""
    monkeypatch.setattr(APP, "TASK_RETRIES", 2)
    monkeypatch.setattr(APP, "TASK_RETRY_DELAY", 60)
    client(monkeypatch, lambda request: httpx.Response(429))
    result = read_public(FetchRequest(source_id="gdelt_doc", query="pneumonia", deadline_seconds=5))
    assert result["records"] == [] and result["error"] is None
    assert result["stop_reason"] == "deadline" and not result["upstream_pagination_exhausted"]
