"""Public-API paging in biz_rules.history_sources._read_public_api: every stop reason and pagination failure,
driven by scripted loopback responses (no network)."""

import json

import httpx
import pytest

from biz_rules import history_sources as sources
from biz_rules.history_models import FetchRequest


def study(nct):
    return {"protocolSection": {
        "identificationModule": {"nctId": nct, "briefTitle": "Trial " + nct},
        "statusModule": {"overallStatus": "RECRUITING", "studyFirstPostDateStruct": {"date": "2020-01"},
                         "startDateStruct": {"date": "2021-02", "type": "ESTIMATED"}},
        "designModule": {"phases": ["PHASE1"]}, "armsInterventionsModule": {"interventions": []}}}


def epmc(*ids):
    return [{"id": value, "source": "MED", "title": "T" + value} for value in ids]


def crl(name):
    return {"file_name": name, "letter_date": "01/01/2020", "letter_type": "COMPLETE RESPONSE",
            "application_number": ["NDA 001"], "text": "x"}


def serve(monkeypatch, responses):
    """Answer successive requests with the scripted (status, json) pairs; a further request is a test error."""
    queue = list(responses)
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        status, body = queue.pop(0)
        return httpx.Response(status, json=body, request=request)

    factory = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(sources.httpx, "Client", lambda **kwargs: factory(transport=transport, **kwargs))
    return seen


def summary(result):
    return {
        "ids": [record.source_record_id for record in result["records"]],
        "stop": result["stop_reason"],
        "error": result["error"]["code"] if result["error"] else None,
        "exhausted": result["upstream_pagination_exhausted"],
        "total": result["source_total"],
        "drift": result["count_drift"],
        "next": result["next_continuation"] and {k: v for k, v in result["next_continuation"].items() if k != "scope_hash"},
    }


CT = dict(source_id="clinicaltrials", query="obesity")
CASES = {
    "ct_two_pages_to_the_end": (CT, [
        (200, {"studies": [study("NCT00000001")], "nextPageToken": "p2", "totalCount": 2}),
        (200, {"studies": [study("NCT00000002")], "totalCount": 2})],
        {"ids": ["NCT00000001", "NCT00000002"], "stop": "upstream_exhausted", "error": None, "exhausted": True,
         "total": 2, "drift": None, "next": None}),
    "ct_page_budget_keeps_the_continuation": (dict(CT, max_pages=1), [
        (200, {"studies": [study("NCT00000001")], "nextPageToken": "p2", "totalCount": 5})],
        {"ids": ["NCT00000001"], "stop": "page_budget", "error": None, "exhausted": False, "total": 5,
         "drift": None, "next": {"cursor": "p2", "read_count": 1}}),
    "ct_record_budget": (dict(CT, max_records=1), [
        (200, {"studies": [study("NCT00000001"), study("NCT00000002")], "nextPageToken": "p2", "totalCount": 9})],
        {"ids": ["NCT00000001"], "stop": "record_budget", "error": None, "exhausted": False, "total": 9,
         "drift": None, "next": {"cursor": "p2", "read_count": 2}}),
    "ct_missing_token_before_total": (CT, [
        (200, {"studies": [study("NCT00000001")], "totalCount": 3})],
        {"ids": ["NCT00000001"], "stop": "source_failed", "error": "invalid_source_pagination", "exhausted": False,
         "total": 3, "drift": None, "next": None}),
    "ct_repeated_token": (CT, [
        (200, {"studies": [study("NCT00000001")], "nextPageToken": "p2", "totalCount": 3}),
        (200, {"studies": [study("NCT00000002")], "nextPageToken": "p2", "totalCount": 3})],
        {"ids": ["NCT00000001", "NCT00000002"], "stop": "source_failed", "error": "invalid_source_pagination",
         "exhausted": False, "total": 3, "drift": None, "next": None}),
    "ct_empty_page_with_token": (CT, [
        (200, {"studies": [], "nextPageToken": "p2", "totalCount": 3})],
        {"ids": [], "stop": "source_failed", "error": "invalid_source_pagination", "exhausted": False, "total": 3,
         "drift": None, "next": None}),
    "ct_invalid_total": (CT, [(200, {"studies": [study("NCT00000001")], "totalCount": -1})],
        {"ids": [], "stop": "source_failed", "error": "invalid_source_pagination", "exhausted": False,
         "total": None, "drift": None, "next": None}),
    "ct_same_id_same_bytes_is_deduplicated": (CT, [
        (200, {"studies": [study("NCT00000001"), study("NCT00000001")], "totalCount": 2})],
        {"ids": ["NCT00000001"], "stop": "upstream_exhausted", "error": None, "exhausted": True, "total": 2,
         "drift": None, "next": None}),
    "ct_same_id_different_bytes_is_refused": (CT, [
        (200, {"studies": [study("NCT00000001"), {**study("NCT00000001"), "x": 1}], "totalCount": 2})],
        {"ids": [], "stop": "source_failed", "error": "ambiguous_native_id", "exhausted": False, "total": 2,
         "drift": None, "next": None}),
    "ct_http_error_is_a_failure": (CT, [(500, {"error": "down"})],
        {"ids": [], "stop": "source_failed", "error": "source_read_failed", "exhausted": False, "total": None,
         "drift": None, "next": None}),
    "epmc_to_the_end": (dict(source_id="europe_pmc", query="a"), [
        (200, {"hitCount": 2, "nextCursorMark": "c2", "resultList": {"result": epmc("1")}}),
        (200, {"hitCount": 2, "nextCursorMark": "c3", "resultList": {"result": epmc("2")}})],
        {"ids": ["MED:1", "MED:2"], "stop": "upstream_exhausted", "error": None, "exhausted": True, "total": 2,
         "drift": None, "next": None}),
    "epmc_page_budget": (dict(source_id="europe_pmc", query="a", max_pages=1), [
        (200, {"hitCount": 9, "nextCursorMark": "c2", "resultList": {"result": epmc("1")}})],
        {"ids": ["MED:1"], "stop": "page_budget", "error": None, "exhausted": False, "total": 9, "drift": None,
         "next": {"cursor": "c2", "read_count": 1}}),
    "epmc_total_must_be_a_count": (dict(source_id="europe_pmc", query="a"), [
        (200, {"hitCount": "9", "nextCursorMark": "c2", "resultList": {"result": epmc("1")}})],
        {"ids": ["MED:1"], "stop": "source_failed", "error": "invalid_source_pagination", "exhausted": False,
         "total": "9", "drift": None, "next": None}),
    "epmc_cursor_repeats_across_pages": (dict(source_id="europe_pmc", query="a"), [
        (200, {"hitCount": 9, "nextCursorMark": "c2", "resultList": {"result": epmc("1")}}),
        (200, {"hitCount": 9, "nextCursorMark": "c2", "resultList": {"result": epmc("2")}})],
        {"ids": ["MED:1", "MED:2"], "stop": "source_failed", "error": "invalid_source_pagination",
         "exhausted": False, "total": 9, "drift": None, "next": None}),
    "crl_total_reached": (dict(source_id="fda_crl", query="a"), [
        (200, {"meta": {"results": {"total": 2}}, "results": [crl("a.pdf"), crl("b.pdf")]})],
        None),
    "crl_no_match_is_an_empty_end": (dict(source_id="fda_crl", query="a"), [
        (404, {"error": {"code": "NOT_FOUND", "message": "No matches found!"}})],
        {"ids": [], "stop": "upstream_no_match", "error": None, "exhausted": True, "total": None, "drift": None,
         "next": None}),
    "crl_other_404_is_a_failure": (dict(source_id="fda_crl", query="a"), [
        (404, {"error": {"code": "OTHER", "message": "x"}})],
        {"ids": [], "stop": "source_failed", "error": "source_read_failed", "exhausted": False, "total": None,
         "drift": None, "next": None}),
    "crl_empty_page_without_total_is_not_eof": (dict(source_id="fda_crl", query="a"), [
        (200, {"results": []})],
        {"ids": [], "stop": "source_failed", "error": "unexpected_empty_page", "exhausted": False, "total": None,
         "drift": None, "next": None}),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_public_api_paging_outcomes(name, monkeypatch):
    kwargs, responses, expected = CASES[name]
    serve(monkeypatch, responses)
    got = summary(sources._read_public_api(FetchRequest(**kwargs)))
    if expected is None:  # crl ids carry a content hash: check shape only
        assert [value.split("#")[0] for value in got.pop("ids")] == ["a.pdf", "b.pdf"]
        expected = {"stop": "upstream_exhausted", "error": None, "exhausted": True, "total": 2, "drift": None,
                    "next": None}
    assert got == expected


def test_continuation_cursor_and_read_count_resume_the_next_page(monkeypatch):
    seen = serve(monkeypatch, [(200, {"studies": [study("NCT00000003")], "totalCount": 3})])
    scope = FetchRequest(**CT).scope_hash()
    result = sources._read_public_api(FetchRequest(
        **CT, continuation={"cursor": "p3", "read_count": 2, "scope_hash": scope}))
    assert seen[0]["pageToken"] == "p3"
    assert summary(result)["exhausted"] is True and summary(result)["ids"] == ["NCT00000003"]


def test_every_page_request_asks_for_the_remaining_budget_and_europe_pmc_starts_at_the_star_cursor(monkeypatch):
    seen = serve(monkeypatch, [
        (200, {"hitCount": 9, "nextCursorMark": "c2", "resultList": {"result": epmc("1", "2")}}),
        (200, {"hitCount": 9, "nextCursorMark": "c3", "resultList": {"result": epmc("3")}})])
    sources._read_public_api(FetchRequest(source_id="europe_pmc", query="a", max_records=3, max_pages=2))
    assert [(call["cursorMark"], call["pageSize"]) for call in seen] == [("*", "3"), ("c2", "1")]
    assert json.dumps(sorted(seen[0])) == json.dumps(sorted(seen[1]))
