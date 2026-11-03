"""固定原件投影和旧链路保护；联网完全用HTTP替身，PG另在integration_history运行。"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from biz_rules import history_sources as sources
from biz_rules.history_models import (
    ArchiveImport,
    ArchiveRecord,
    FetchRequest,
    TimeValue,
)


def trial_raw(phase="PHASE1"):
    return {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT01234567", "briefTitle": "Trial"},
            "statusModule": {
                "overallStatus": "RECRUITING",
                "studyFirstPostDateStruct": {"date": "2020-01"},
                "startDateStruct": {"date": "2021-02", "type": "ESTIMATED"},
            },
            "designModule": {"phases": [phase]},
            "armsInterventionsModule": {"interventions": []},
        }
    }


def test_trial_exact_facts_precision_and_missing_unknown():
    raw = json.dumps(trial_raw(), ensure_ascii=False)
    record = ArchiveRecord(
        source_record_id="NCT01234567", record_kind="trial", raw_content=raw
    )
    result = sources.project_record("clinicaltrials", record)
    assert result["facts"]["phase"] == ["PHASE1"]
    assert result["facts"]["interventions"] == []
    assert "enrollment" not in result["facts"]
    assert (
        result["field_locators"]["phase"]["json_pointer"]
        == "/protocolSection/designModule/phases"
    )
    assert result["source_published_at"]["precision"] == "month"
    assert result["event_time"]["meaning"] == "registry_start_ESTIMATED"


def test_duplicate_json_or_mismatched_native_rejected():
    with pytest.raises(Exception, match="来源JSON"):
        sources.parse_json('{"id":1,"id":2}')
    with pytest.raises(Exception, match="原件不一致"):
        sources.project_record(
            "clinicaltrials",
            ArchiveRecord(
                source_record_id="NCT99999999",
                record_kind="trial",
                raw_content=json.dumps(trial_raw()),
            ),
        )


def test_crl_multiple_letters_same_pdf_do_not_collide_or_infer_failure():
    first = {
        "file_name": "one.pdf",
        "letter_date": "01/01/2020",
        "letter_type": "COMPLETE RESPONSE",
        "application_number": ["NDA 001"],
        "text": "Deficiency",
    }
    second = {
        **first,
        "letter_date": "01/01/2021",
        "letter_type": "APPROVAL",
        "text": "Approved",
    }
    assert sources.crl_id(first) != sources.crl_id(second)
    result = sources.project_record(
        "fda_crl",
        ArchiveRecord(
            source_record_id=sources.crl_id(second),
            record_kind="document",
            raw_content=json.dumps(second),
        ),
    )
    assert result["facts"]["letter_type"] == "APPROVAL"
    assert result["facts"]["application_numbers"] == ["NDA 001"]
    assert result["source_published_at"] is None


def test_epmc_preserves_cross_source_ids_corrections_and_license_without_fulltext():
    data = {
        "id": "123",
        "source": "MED",
        "pmid": "123",
        "doi": "10.1/a",
        "title": "Correction",
        "pubTypeList": {"pubType": ["Published Erratum"]},
        "commentCorrectionList": {
            "commentCorrection": [{"id": "122", "source": "MED"}]
        },
        "isOpenAccess": "N",
        "firstPublicationDate": "2020-01-01",
    }
    result = sources.project_record(
        "europe_pmc",
        ArchiveRecord(
            source_record_id="MED:123",
            record_kind="document",
            raw_content=json.dumps(data),
        ),
    )
    assert result["facts"]["pmid"] == "123"
    assert result["facts"]["is_open_access"] == "N"
    assert "license" not in result["facts"]
    assert result["field_locators"]["correction_links"] == {
        "json_pointer": "/commentCorrectionList"
    }


def test_outcomes_require_explicit_negative_coverage():
    data = {
        "record_kind": "outcome",
        "title": "negative",
        "event_type": "trial_stoppage",
        "unit_kind": "trial",
        "unit_id": "T1",
        "event_observed": False,
    }
    with pytest.raises(Exception, match="观察窗口"):
        sources.project_record(
            "external_archive",
            ArchiveRecord(
                source_record_id="O1",
                record_kind="outcome",
                raw_content=json.dumps(data),
            ),
        )
    data.update(
        observation_start="2020-01-01",
        observation_end="2020-12-31",
        public_available_at="2021-01-01",
    )
    result = sources.project_record(
        "external_archive",
        ArchiveRecord(
            source_record_id="O1", record_kind="outcome", raw_content=json.dumps(data)
        ),
    )
    assert result["facts"]["event_observed"] is False
    assert result["field_locators"]["event_observed"] == {
        "json_pointer": "/event_observed"
    }


def test_naive_instants_and_client_receipt_forbidden():
    with pytest.raises(ValueError):
        TimeValue(value="2020-01-01T00:00:00", precision="instant")
    with pytest.raises(ValueError):
        ArchiveImport(
            source_id="external_archive",
            is_demo=True,
            scope_description="demo",
            declared_count=0,
            records=[],
            first_seen_at="2016-01-01T00:00:00Z",
        )


def test_page_raw_slice_preserves_unicode_and_whitespace():
    text = '{ "results" : [ { "title": "药物甲", "text":"a\\nb" }, {"n":2} ] }'
    parts = list(sources._raw_array_items(text, ["results"]))
    assert parts[0][1] == '{ "title": "药物甲", "text":"a\\nb" }'
    assert json.loads(parts[0][1]) == parts[0][0]


def test_public_transport_failure_is_failure_not_empty_eof(monkeypatch):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"error": "down"}, request=request)
    )
    factory = httpx.Client
    monkeypatch.setattr(
        sources.httpx,
        "Client",
        lambda transport=transport, **kwargs: factory(transport=transport, **kwargs),
    )
    result = sources.read_public(FetchRequest(source_id="europe_pmc", query="obesity"))
    assert result["error"]["code"] == "source_read_failed"
    assert result["records"] == []
    assert result["upstream_pagination_exhausted"] is False


def test_public_page_budget_is_partial_and_raw_original(monkeypatch):
    body = '{ "nextCursorMark":"next", "hitCount":100, "resultList": { "result":[ { "id":"1","source":"MED","title":"Alpha" } ] } }'
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=body, request=request)
    )
    factory = httpx.Client
    monkeypatch.setattr(
        sources.httpx,
        "Client",
        lambda transport=transport, **kwargs: factory(transport=transport, **kwargs),
    )
    result = sources.read_public(
        FetchRequest(source_id="europe_pmc", query="alpha", max_records=1)
    )
    assert result["stop_reason"] == "record_budget"
    assert not result["complete"] and not result["upstream_pagination_exhausted"]
    assert (
        result["records"][0].raw_content
        == '{ "id":"1","source":"MED","title":"Alpha" }'
    )


def test_legacy_clinicaltrials_failure_and_truncation_not_eof(monkeypatch):
    import requests

    from biz_rules.extractors import clinicaltrials_fetcher as fetcher

    monkeypatch.setattr(
        fetcher.requests, "get", Mock(side_effect=requests.ConnectionError())
    )
    with pytest.raises(RuntimeError, match="不能视为源已读完"):
        list(fetcher.fetch_clinicaltrials({}))
    response = Mock()
    response.json.return_value = {"studies": [trial_raw()], "nextPageToken": "later"}
    response.raise_for_status.return_value = None
    monkeypatch.setattr(fetcher.requests, "get", lambda *args, **kwargs: response)
    iterator = fetcher.fetch_clinicaltrials({"max_pages": 1})
    assert (
        next(iterator)["protocolSection"]["identificationModule"]["nctId"]
        == "NCT01234567"
    )
    with pytest.raises(RuntimeError, match="已截断"):
        next(iterator)


def test_legacy_pubmed_real_abstract_and_error(monkeypatch):
    from biz_rules.extractors.pubmed import pubmed_fetcher as fetcher

    xml = '<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article><Abstract><AbstractText Label="RESULTS">Real <b>abstract</b>.</AbstractText></Abstract><PublicationTypeList><PublicationType>Retracted Publication</PublicationType></PublicationTypeList></Article><CommentsCorrectionsList><CommentsCorrections RefType="RetractionIn"><PMID>124</PMID></CommentsCorrections></CommentsCorrectionsList></MedlineCitation></PubmedArticle></PubmedArticleSet>'
    response = Mock(content=xml.encode(), text=xml)
    response.raise_for_status.return_value = None
    monkeypatch.setattr(fetcher.httpx, "get", lambda *args, **kwargs: response)
    detail = fetcher._fetch_pubmed_details(["123"])["123"]
    assert detail["abstract"] == "Real abstract."
    assert detail["publication_types"] == ["Retracted Publication"]
    assert detail["correction_links"][0]["pmid"] == "124"
    assert (
        fetcher._normalize("123", {"summary": "Wrong esummary field"})["abstract"] == ""
    )
    monkeypatch.setattr(
        fetcher.httpx, "get", Mock(side_effect=httpx.ConnectError("offline"))
    )
    with pytest.raises(RuntimeError, match="不能视为零命中"):
        list(fetcher.iter_pubmed({"query": "x", "max_records": 2}))


def test_unknown_enrollment_not_zero():
    from biz_rules.schemas import ClinicalTrialsExtra
    from core_engine.api_router import _extract_extra_value

    assert _extract_extra_value({}, {"path": "n", "type": "int"}) is None
    assert _extract_extra_value({"n": 0}, {"path": "n", "type": "int"}) == 0
    assert _extract_extra_value({"n": True}, {"path": "n", "type": "int"}) is None
    assert ClinicalTrialsExtra().enrollment is None


def test_legacy_no_database_is_not_saved(monkeypatch):
    import db.session
    from db import repository

    @contextmanager
    def absent():
        yield None

    monkeypatch.setattr(db.session, "get_session", absent)
    with pytest.raises(RuntimeError, match="未保存"):
        repository.upsert_document(SimpleNamespace())


def test_legacy_api_recrawl_does_not_reset_or_fetch(monkeypatch):
    import db.repository
    from flows import crawl_flow as flow

    doc = SimpleNamespace(
        source_url="https://clinicaltrials.gov/api/v2/studies",
        source_site="clinicaltrials",
    )
    monkeypatch.setattr(flow, "get_document_by_uuid", lambda value: doc)
    monkeypatch.setattr(db.repository, "log_crawl_task", lambda **kwargs: 1)
    statuses = []
    monkeypatch.setattr(
        db.repository,
        "update_crawl_log",
        lambda *args, **kwargs: statuses.append((args, kwargs)),
    )
    monkeypatch.setattr(
        db.repository,
        "reset_document_for_recrawl",
        Mock(side_effect=AssertionError("must not reset")),
    )
    monkeypatch.setattr(
        flow.fetch_and_parse_url,
        "fn",
        Mock(side_effect=AssertionError("must not html fetch")),
    )
    result = flow.recrawl_document_fn("old")
    assert result["success"] is False
    assert statuses[-1][0][1] == "failed"


def test_legacy_changed_text_rejected_before_old_result_write(monkeypatch):
    import db.session
    from db import repository

    document = SimpleNamespace(raw_content="old raw", extraction_status="completed")
    session = Mock()
    session.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = document

    @contextmanager
    def existing():
        yield session

    monkeypatch.setattr(db.session, "get_session", existing)
    with pytest.raises(RuntimeError, match="旧结果已保留"):
        repository.upsert_document(
            SimpleNamespace(uuid="old", raw_content="changed raw")
        )
    assert (
        document.raw_content == "old raw" and document.extraction_status == "completed"
    )
    session.flush.assert_not_called()


def test_epmc_repeated_or_missing_cursor_is_not_eof(monkeypatch):
    factory = httpx.Client
    for cursor in ("*", None):
        body = {
            "nextCursorMark": cursor,
            "hitCount": 100,
            "resultList": {"result": [{"id": "1", "source": "MED", "title": "Alpha"}]},
        }
        transport = httpx.MockTransport(
            lambda request, body=body: httpx.Response(200, json=body, request=request)
        )
        monkeypatch.setattr(
            sources.httpx,
            "Client",
            lambda transport=transport, **kwargs: factory(
                transport=transport, **kwargs
            ),
        )
        result = sources.read_public(
            FetchRequest(source_id="europe_pmc", query="alpha", max_records=10)
        )
        assert result["error"]["code"] == "invalid_source_pagination"
        assert len(result["records"]) == 1
        assert not result["upstream_pagination_exhausted"]


def test_epmc_end_after_window_shrinks_is_eof_with_drift(monkeypatch):
    """Live 2026-11-03: 716 of 780 read, then hitCount 720 and an empty page without a cursor."""
    factory = httpx.Client
    body = {"hitCount": 720, "resultList": {"result": []}}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=body, request=request)
    )
    monkeypatch.setattr(
        sources.httpx,
        "Client",
        lambda transport=transport, **kwargs: factory(transport=transport, **kwargs),
    )
    scope = FetchRequest(source_id="europe_pmc", query="alpha").scope_hash()
    request = FetchRequest(
        source_id="europe_pmc",
        query="alpha",
        continuation={"cursor": "AoIo", "read_count": 716, "scope_hash": scope},
    )
    result = sources.read_public(request)
    assert result["error"] is None and result["upstream_pagination_exhausted"] is True
    assert result["count_drift"] == {"source_total": 720, "read_count": 716}
    assert result["next_continuation"] is None


def test_epmc_empty_first_page_with_hits_is_not_eof(monkeypatch):
    factory = httpx.Client
    body = {"hitCount": 5, "resultList": {"result": []}}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=body, request=request)
    )
    monkeypatch.setattr(
        sources.httpx,
        "Client",
        lambda transport=transport, **kwargs: factory(transport=transport, **kwargs),
    )
    result = sources.read_public(FetchRequest(source_id="europe_pmc", query="alpha"))
    assert result["error"]["code"] == "invalid_source_pagination"
    assert not result["upstream_pagination_exhausted"]


def test_clinicaltrials_daily_data_date_is_not_a_record_change():
    """Live 2026-11-03: every refresh made a new version of all 516 trials; only versionHolder differed."""
    import json

    def raw(holder, status="RECRUITING"):
        return json.dumps({"protocolSection": {"identificationModule": {"nctId": "NCT01234567"},
                                               "statusModule": {"overallStatus": status}},
                           "derivedSection": {"miscInfoModule": {"versionHolder": holder}}})

    assert sources.stable_raw("clinicaltrials", raw("2026-11-02")) == sources.stable_raw("clinicaltrials", raw("2026-11-03"))
    assert sources.stable_raw("clinicaltrials", raw("2026-11-02")) != sources.stable_raw(
        "clinicaltrials", raw("2026-11-03", status="COMPLETED"))
    assert sources.stable_raw("europe_pmc", raw("2026-11-02")) is None
    assert sources.stable_raw("clinicaltrials", "not json") is None
