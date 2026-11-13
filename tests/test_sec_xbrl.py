"""SEC原件/单位/期间的定向回归；全部金额为明确合成fixture，HTTP不联网。"""

import json
from types import SimpleNamespace

import httpx
import pytest

from fetch import sec_xbrl as sec
from fetch.projection import _pointer, project_record
from fetch.sources import read_public
from store.history_models import ArchiveRecord, FetchRequest, HistoryError

NATIVE = "CIK0001682852:us-gaap:NetIncomeLoss"


def value():
    return {
        "cik": 1682852, "taxonomy": "us-gaap", "tag": "NetIncomeLoss",
        "entityName": "SYNTHETIC company", "label": "SYNTHETIC net income",
        "description": "SYNTHETIC reported figure, not a real company financial value",
        "units": {"USD": [
            {"val": -25, "start": "2020-01-01", "end": "2020-12-31", "filed": "2021-03-01",
             "fy": 2020, "fp": "FY", "form": "10-K", "accn": "0001682852-21-000001", "frame": "CY2020"},
            # 同一事实期间在后来申报中再次披露；不得按fy覆盖期间或默默去重。
            {"val": 0, "start": "2020-01-01", "end": "2020-12-31", "filed": "2022-03-01",
             "fy": 2021, "fp": "FY", "form": "10-K", "accn": "0001682852-22-000001"},
        ]},
    }


def record(data=None, native=NATIVE):
    return ArchiveRecord(source_record_id=native, record_kind="document",
                         raw_content=json.dumps(value() if data is None else data))


def test_native_financial_rows_keep_negative_zero_repeated_filing_and_dates():
    item = record()
    result = project_record("sec_companyfacts", item)
    rows = result["facts"]["financial_observations"]
    assert [row["val"] for row in rows] == [-25, 0]
    assert rows[1]["fy"] == 2021 and rows[1]["end"] == "2020-12-31"
    assert rows[0]["filed"] != rows[1]["filed"]
    assert result["source_published_at"] is None and result["source_updated_at"] is None
    assert result["event_time"] is None
    assert result["parser_version"] == sec.SEC_PARSER
    assert "companyconcept/CIK0001682852/us-gaap/NetIncomeLoss.json" in result["source_url"]
    raw = json.loads(item.raw_content)
    for row in rows:
        assert _pointer(raw, row["source_row_pointer"])["val"] == row["val"]
        assert row["unit_locator"] == {"json_pointer": "/units", "object_key": "USD"}
        for field, locator in row["field_locators"].items():
            assert _pointer(raw, locator["json_pointer"]) == row[field]
        assert "denominator" not in row and "consolidation_scope" not in row
    assert item.raw_content == json.dumps(value())
    assert "first_seen_at" not in result and "received_at" not in result


def test_unit_currency_and_ratio_units_are_not_coerced_or_combined():
    data = value()
    data["units"]["EUR"] = [dict(data["units"]["USD"][0])]
    data["units"]["USD/shares"] = [dict(data["units"]["USD"][0], val=0.125)]
    rows = project_record("sec_companyfacts", record(data))["facts"]["financial_observations"]
    assert [r["unit"] for r in rows] == ["USD", "USD", "EUR", "USD/shares"]
    assert rows[-1]["val"] == 0.125
    assert rows[-1]["field_locators"]["val"] == {"json_pointer": "/units/USD~1shares/0/val"}
    assert _pointer(data, rows[-1]["field_locators"]["val"]["json_pointer"]) == 0.125


def test_missing_and_explicit_null_optional_financial_fields_are_distinct():
    data = value()
    data["units"]["USD"][0]["frame"] = None
    rows = project_record("sec_companyfacts", record(data))["facts"]["financial_observations"]
    assert rows[0]["frame"] is None and "frame" in rows[0]["field_locators"]
    assert "frame" not in rows[1] and "frame" not in rows[1]["field_locators"]


@pytest.mark.parametrize("change", [
    {"val": True}, {"val": None}, {"val": "100"}, {"val": float("inf")},
    {"start": "2021-01-01"}, {"filed": "2021"}, {"end": None}, {"accn": "guessed"},
])
def test_invalid_financial_numbers_dates_and_accessions_are_not_success(change):
    data = value()
    data["units"]["USD"][0].update(change)
    with pytest.raises(HistoryError):
        project_record("sec_companyfacts", record(data))


@pytest.mark.parametrize("native", [
    "1682852:us-gaap:NetIncomeLoss", "CIK0000000000:us-gaap:NetIncomeLoss",
    "CIK0001682852:us-gaap:../../escape", "https://untrusted.invalid/file.json",
    "CIK0001682852:custom:NetIncomeLoss",
])
def test_source_scope_rejects_guessed_or_external_endpoints(native):
    with pytest.raises(HistoryError):
        sec.split_native(native)


def request(**change):
    return FetchRequest(source_id="sec_companyfacts", query="*", source_record_ids=[NATIVE], **change)


def mocked_http(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(sec, "httpx", SimpleNamespace(
        Client=lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
        HTTPError=httpx.HTTPError,
    ))


def test_bounded_reader_preserves_exact_complete_http_original_and_no_hidden_retry(monkeypatch):
    original = json.dumps(value(), indent=2)
    calls = []
    def respond(req):
        calls.append(req)
        assert req.url.host == "data.sec.gov" and "companyconcept" in req.url.path
        assert req.headers["user-agent"].startswith("BioRadar-")
        return httpx.Response(200, text=original)
    mocked_http(monkeypatch, respond)
    batch = read_public(request())
    assert batch["error"] is None and batch["upstream_pagination_exhausted"] is True
    assert batch["complete"] is False and batch["source_total"] is None
    assert batch["records"][0].raw_content == original and len(calls) == 1


@pytest.mark.parametrize("kind", ["timeout", "404", "503", "wrong_identity"])
def test_failure_is_not_zero_or_a_legal_empty_disclosure(monkeypatch, kind):
    calls = []
    def respond(req):
        calls.append(req)
        if kind == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC timeout", request=req)
        if kind in {"404", "503"}:
            return httpx.Response(int(kind))
        return httpx.Response(200, json={**value(), "cik": 1})
    mocked_http(monkeypatch, respond)
    from settings import APP

    monkeypatch.setattr(APP, "TASK_RETRIES", 1)
    batch = read_public(request())
    assert batch["error"] and not batch["upstream_pagination_exhausted"]
    assert batch["stop_reason"] == "source_failed" and batch["records"] == []
    assert len(calls) == (2 if kind == "timeout" else 1)  # 超时在传输层统一有界重试一次；其他错误不重试


def test_page_budget_is_explicit_and_retains_success_before_later_error(monkeypatch):
    ids = [NATIVE, "CIK0001682852:us-gaap:Assets"]
    calls = []
    def respond(req):
        calls.append(req)
        return httpx.Response(200, json=value()) if len(calls) == 1 else httpx.Response(503)
    mocked_http(monkeypatch, respond)
    from fetch import transport

    monkeypatch.setattr(transport.time, "sleep", lambda _: None)
    limited = read_public(FetchRequest(source_id="sec_companyfacts", query="*", source_record_ids=ids,
                                     max_pages=1, max_records=2))
    assert limited["stop_reason"] == "page_budget" and not limited["upstream_pagination_exhausted"]
    assert len(limited["records"]) == 1 and limited["error"] is None
    calls.clear()
    partial = read_public(FetchRequest(source_id="sec_companyfacts", query="*", source_record_ids=ids,
                                     max_pages=2, max_records=2))
    assert partial["error"] and len(partial["records"]) == 1 and len(calls) == 2


def test_reader_requires_explicit_native_scope_before_network(monkeypatch):
    mocked_http(monkeypatch, lambda _: pytest.fail("No HTTP without exact scope"))
    with pytest.raises(HistoryError, match="清单"):
        read_public(FetchRequest(source_id="sec_companyfacts", query="company name"))
