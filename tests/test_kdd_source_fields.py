"""KDD原生字段、缺失与分页合同；数据均为显式合成测试，不代表实际研究成绩。"""

import json

import httpx
import pytest

from fetch import sources
from store.record_models import ArchiveRecord, FetchRequest
from tests.test_records import trial_raw
from tests.test_source_refresh import who_value


def test_ct_preserves_design_denominators_results_and_missing_without_judging_success():
    data = trial_raw()
    protocol = data["protocolSection"]
    protocol["statusModule"].update({"overallStatus": "TERMINATED", "whyStopped": "SYNTHETIC slow accrual"})
    protocol["designModule"].update({
        "studyType": "INTERVENTIONAL", "enrollmentInfo": {"count": 0, "type": "ACTUAL"},
        "designInfo": {"allocation": "RANDOMIZED", "primaryPurpose": "TREATMENT"},
    })
    protocol["sponsorCollaboratorsModule"] = {"leadSponsor": {"name": "SYNTHETIC sponsor", "class": "INDUSTRY"}}
    protocol["eligibilityModule"] = {"sex": "ALL", "minimumAge": "18 Years", "healthyVolunteers": False}
    protocol["outcomesModule"] = {"primaryOutcomes": [{"measure": "SYNTHETIC response", "timeFrame": "6 months"}]}
    data["hasResults"] = False
    data["resultsSection"] = {"outcomeMeasuresModule": {"outcomeMeasures": [{
        "title": "SYNTHETIC response", "unitOfMeasure": "Participants", "denomUnitsSelected": "PARTICIPANTS",
        "denoms": [{"units": "Participants", "counts": [{"groupId": "G1", "value": "0"}]}],
    }]}}
    item = ArchiveRecord(source_record_id="NCT01234567", record_kind="trial", raw_content=json.dumps(data))
    projected = sources.project_record("clinicaltrials", item)
    facts = projected["facts"]
    assert facts["enrollment"] == 0 and facts["enrollment_type"] == "ACTUAL"
    assert facts["has_results"] is False and facts["eligibility"]["healthyVolunteers"] is False
    assert facts["status"] == "TERMINATED" and facts["why_stopped"] == "SYNTHETIC slow accrual"
    assert "clinical_failure" not in facts and "primary_completion_date" not in facts
    assert facts["results_outcome_measures"]["outcomeMeasures"][0]["denoms"][0]["counts"][0]["value"] == "0"
    assert projected["parser_version"] == "datafabric.clinicaltrials.native.v0.2"
    for field, locator in projected["field_locators"].items():
        assert sources._pointer(data, locator["json_pointer"]) == facts[field]


def test_epmc_preserves_native_types_authors_grants_and_correction_without_independence_claim():
    data = {
        "id": "123", "source": "MED", "title": "SYNTHETIC correction",
        "pmid": "123", "doi": "10.synthetic/id", "firstPublicationDate": "2020-01-01",
        "authorList": {"author": [{"fullName": "SYNTHETIC researcher"}]},
        "grantsList": {"grant": [{"grantId": "SYNTHETIC-GRANT"}]},
        "pubTypeList": {"pubType": ["Retraction of Publication"]},
        "commentCorrectionList": {"commentCorrection": [{"source": "MED", "id": "122", "type": "Retraction of"}]},
        "keywordList": None, "fullTextUrlList": {"fullTextUrl": []}, "isOpenAccess": "N",
    }
    item = ArchiveRecord(source_record_id="MED:123", record_kind="document", raw_content=json.dumps(data))
    projected = sources.project_record("europe_pmc", item)
    assert projected["facts"]["authors"] == data["authorList"]
    assert projected["facts"]["grants"] == data["grantsList"]
    assert projected["facts"]["keywords"] is None and "chemicals" not in projected["facts"]
    assert projected["facts"]["correction_links"]["commentCorrection"][0]["id"] == "122"
    assert "independent_study" not in projected["facts"] and "mechanism_confirmed" not in projected["facts"]
    assert projected["parser_version"] == "datafabric.europepmc.core.v0.2"
    for field, locator in projected["field_locators"].items():
        assert sources._pointer(data, locator["json_pointer"]) == projected["facts"][field]


def test_who_keeps_notice_mode_without_invented_population_rate_or_cases():
    data = who_value()
    data["Summary"] = "<p>SYNTHETIC narrative mentions 100 cases; denominator absent.</p>"
    projected = sources.project_record("who_don", ArchiveRecord(
        source_record_id=data["Id"], record_kind="document", raw_content=json.dumps(data)))
    assert not {"cases", "population", "denominator", "incidence", "rate"} & projected["facts"].keys()
    assert projected["event_time"]["meaning"] == "who_notice_publication"


def test_ct_persistent_continuation_keeps_native_token_scope_and_zero_failures(monkeypatch):
    real = httpx.Client
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"studies": [trial_raw()], "totalCount": 2, "nextPageToken": "opaque-next"})
        assert request.url.params["pageToken"] == "opaque-next"
        data = trial_raw()
        data["protocolSection"]["identificationModule"]["nctId"] = "NCT01234568"
        return httpx.Response(200, json={"studies": [data], "totalCount": 2})
    monkeypatch.setattr(sources.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(respond), **kwargs))
    first = FetchRequest(source_id="clinicaltrials", query="SYNTHETIC fixed date window", max_records=1, max_pages=1)
    one = sources.read_public(first)
    assert one["next_continuation"]["cursor"] == "opaque-next" and not one["upstream_pagination_exhausted"]
    assert one["source_total"] == 2 and one["next_continuation"]["read_count"] == 1
    second = FetchRequest(**{**first.model_dump(), "continuation": one["next_continuation"]})
    two = sources.read_public(second)
    assert two["error"] is None and two["upstream_pagination_exhausted"]
    assert two["next_continuation"] is None and two["records"][0].source_record_id == "NCT01234568"


def test_ct_repeated_page_token_is_error_even_at_one_record_budget(monkeypatch):
    real = httpx.Client
    monkeypatch.setattr(sources.httpx, "Client", lambda **kwargs: real(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"studies": [trial_raw()], "nextPageToken": "same-token"})
    ), **kwargs))
    request = FetchRequest(source_id="clinicaltrials", query="SYNTHETIC", max_records=1)
    request = FetchRequest(**{**request.model_dump(), "continuation": {
        "cursor": "same-token", "read_count": 1,
    }})
    batch = sources.read_public(request)
    assert batch["error"] and batch["stop_reason"] == "source_failed"
    assert not batch["upstream_pagination_exhausted"] and batch["next_continuation"] is None


def ct_batch(monkeypatch, payload, *, consumed=0, max_records=10):
    real = httpx.Client
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(sources.httpx, "Client", lambda **kwargs: real(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    request = FetchRequest(source_id="clinicaltrials", query="SYNTHETIC", max_records=max_records)
    if consumed:
        request = FetchRequest(**{**request.model_dump(), "continuation": {
            "cursor": "prior-token", "read_count": consumed,
        }})
    return sources.read_public(request), calls


@pytest.mark.parametrize("total", [-1, True, False, "2", 2.0, None])
def test_ct_explicit_invalid_total_is_failure_not_exhaustion(monkeypatch, total):
    batch, calls = ct_batch(monkeypatch, {"studies": [trial_raw()], "totalCount": total})
    assert batch["error"]["code"] == "invalid_source_pagination"
    assert batch["stop_reason"] == "source_failed"
    assert not batch["upstream_pagination_exhausted"] and batch["next_continuation"] is None
    assert len(calls) == 1


@pytest.mark.parametrize("consumed,total", [(0, 2), (7, 9)])
def test_ct_missing_page_token_cannot_hide_declared_remaining_records(monkeypatch, consumed, total):
    batch, calls = ct_batch(monkeypatch, {"studies": [trial_raw()], "totalCount": total},
                            consumed=consumed, max_records=1)
    assert batch["error"]["code"] == "invalid_source_pagination"
    assert batch["stop_reason"] == "source_failed"
    assert not batch["upstream_pagination_exhausted"] and batch["next_continuation"] is None
    assert batch["records"][0].source_record_id == "NCT01234567"
    assert batch["source_total"] == total and len(calls) == 1


@pytest.mark.parametrize("consumed,total", [(0, 1), (7, 8)])
def test_ct_valid_final_page_counts_prior_continuation(monkeypatch, consumed, total):
    batch, calls = ct_batch(monkeypatch, {"studies": [trial_raw()], "totalCount": total},
                            consumed=consumed)
    assert batch["error"] is None and batch["upstream_pagination_exhausted"]
    assert batch["stop_reason"] == "upstream_exhausted" and batch["next_continuation"] is None
    assert len(batch["records"]) == 1 and len(calls) == 1


def test_ct_zero_total_empty_final_page_is_legal(monkeypatch):
    batch, calls = ct_batch(monkeypatch, {"studies": [], "totalCount": 0})
    assert batch["error"] is None and batch["upstream_pagination_exhausted"]
    assert batch["stop_reason"] == "upstream_exhausted" and not batch["records"]
    assert len(calls) == 1


def test_ct_missing_optional_total_preserves_token_eof_contract(monkeypatch):
    batch, calls = ct_batch(monkeypatch, {"studies": [trial_raw()]})
    assert batch["error"] is None and batch["upstream_pagination_exhausted"]
    assert batch["source_total"] is None and len(calls) == 1


def test_ct_empty_page_with_token_stops_after_one_request(monkeypatch):
    batch, calls = ct_batch(monkeypatch, {
        "studies": [], "totalCount": 2, "nextPageToken": "unexpected-next",
    })
    assert batch["error"]["code"] == "invalid_source_pagination"
    assert batch["stop_reason"] == "source_failed"
    assert not batch["upstream_pagination_exhausted"] and batch["next_continuation"] is None
    assert len(calls) == 1  # Never spins through changing tokens from an empty source page.


@pytest.mark.parametrize("query", ["*", " * ", "lung cancer"])
def test_clinical_native_ids_do_not_send_literal_wildcard(monkeypatch, query):
    import httpx

    from fetch.sources import read_public
    from store.record_models import FetchRequest

    real = httpx.Client

    def handler(request):
        assert request.url.params["filter.ids"] == "NCT00000001"
        if query.strip() == "*":
            assert "query.term" not in request.url.params
        else:
            assert request.url.params["query.term"] == query
        return httpx.Response(200, json={"totalCount": 1, "studies": [{
            "protocolSection": {"identificationModule": {
                "nctId": "NCT00000001", "briefTitle": "SYNTHETIC fixed-id study",
            }},
        }]})

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    result = read_public(FetchRequest(source_id="clinicaltrials", query=query,
                                     source_record_ids=["NCT00000001"], max_records=5))
    assert result["error"] is None and result["upstream_pagination_exhausted"]
    assert [item.source_record_id for item in result["records"]] == ["NCT00000001"]
