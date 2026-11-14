"""固定原件投影和旧链路保护；联网完全用HTTP替身，PG另在integration_history运行。"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from fetch import sources
from store.history_models import (
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


def test_naive_instants_are_not_a_time():
    with pytest.raises(ValueError):
        TimeValue(value="2020-01-01T00:00:00", precision="instant")


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


def test_unknown_enrollment_not_zero():
    from fetch.api_fields import _extract_extra_value
    from graph.db.schemas import ClinicalTrialsExtra

    assert _extract_extra_value({}, {"path": "n", "type": "int"}) is None
    assert _extract_extra_value({"n": 0}, {"path": "n", "type": "int"}) == 0
    assert _extract_extra_value({"n": True}, {"path": "n", "type": "int"}) is None
    assert ClinicalTrialsExtra().enrollment is None


def test_legacy_no_database_is_not_saved(monkeypatch):
    import graph.db.session
    from graph.db import repository

    @contextmanager
    def absent():
        yield None

    monkeypatch.setattr(graph.db.session, "get_session", absent)
    with pytest.raises(RuntimeError, match="未保存"):
        repository.upsert_document(SimpleNamespace())


def test_legacy_changed_text_rejected_before_old_result_write(monkeypatch):
    import graph.db.session
    from graph.db import repository

    document = SimpleNamespace(raw_content="old raw", extraction_status="completed")
    session = Mock()
    session.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = document

    @contextmanager
    def existing():
        yield session

    monkeypatch.setattr(graph.db.session, "get_session", existing)
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
    request = FetchRequest(
        source_id="europe_pmc",
        query="alpha",
        continuation={"cursor": "AoIo", "read_count": 716},
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


@pytest.mark.parametrize(("stop_reason", "state"), [
    ("upstream_exhausted", "completed"),
    ("record_budget", "partial"), ("page_budget", "partial"), ("deadline", "partial"),
    ("reached_known", "completed"),
])
def test_a_fetch_that_stopped_at_a_budget_is_partial_not_completed(stop_reason, state):
    from datetime import datetime, timezone

    from store import history

    written = []
    conn = SimpleNamespace(execute=lambda statement, params: written.append(params))
    run = {"id": "r", "attempt": 1, "kind": "fetch", "attempts": []}
    history._close_run(conn, run, [], [], {"stop_reason": stop_reason}, None, datetime.now(timezone.utc))
    assert written[0]["state"] == state
