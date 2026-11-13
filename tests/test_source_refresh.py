"""固定观测契约/官方公告协议反例；HTTP替身不冒充线上采集。"""

import json

import httpx
import pytest

from fetch import rate
from fetch.sources import read_public
from fetch.who_don import project_who
from store.history_models import ArchiveRecord, FetchRequest


def who_value(number=1):
    return {
        "Id": f"00000000-0000-4000-8000-{number:012d}",
        "DonId": "SYNTHETIC-DON-" + str(number),
        "Title": "SYNTHETIC outbreak notice",
        "PublicationDate": "2026-10-10T12:00:00Z",
        "PublicationDateAndTime": "2026-10-10T11:00:00Z",
        "LastModified": f"2026-10-11T12:00:{number:02d}Z",
        "Summary": "<p>Declared notice, no inferred numeric incidence.</p>",
        "UrlName": "synthetic-notice",
    }


def test_who_projection_is_notice_publication_not_outbreak_start_or_metric():
    data = who_value()
    item = ArchiveRecord(
        source_record_id=data["Id"],
        record_kind="document",
        raw_content=json.dumps(data),
    )
    value = project_who(item, data)
    assert value["event_time"]["meaning"] == "who_notice_publication"
    assert value["facts"]["native_notice_id"] == data["Id"]
    assert value["source_published_at"]["value"] == data["PublicationDateAndTime"]
    assert "<p>" not in value["clean_text"] and "incidence" not in value["facts"]
    assert value["field_locators"]["last_modified"] == {"json_pointer": "/LastModified"}


REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    """The 30 s spacing of the who.int gate is not under test here."""
    monkeypatch.setattr(rate, "wait_for_url", lambda url, **kw: None)


def mocked_client(monkeypatch, handler):
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
    )


def test_who_resumes_fixed_scope_and_http_failure_never_zero_success(monkeypatch):
    def handler(request):
        assert request.url.host == "www.who.int"
        after = 3 if "Id gt" in request.url.params.get("$filter", "") else 1
        assert "$skip" not in request.url.params
        return httpx.Response(
            200, json={"value": [who_value(i) for i in range(after, 5)]}
        )

    mocked_client(monkeypatch, handler)
    request = FetchRequest(source_id="who_don", query="*", max_records=2, max_pages=1)
    first = read_public(request)
    assert not first["upstream_pagination_exhausted"] and len(first["records"]) == 2
    second_request = FetchRequest.model_validate(
        {**request.model_dump(mode="json"), "continuation": first["next_continuation"]}
    )
    second = read_public(second_request)
    assert second["upstream_pagination_exhausted"] and len(second["records"]) == 2
    assert not {v.source_record_id for v in first["records"]} & {
        v.source_record_id for v in second["records"]
    }
    # HTTP 403 is not an exhausted empty query.
    mocked_client(monkeypatch, lambda request: httpx.Response(403))
    failure = read_public(request)
    assert failure["error"] and not failure["upstream_pagination_exhausted"]


def test_epmc_continuation_survives_record_budget_and_keeps_total_offset(monkeypatch):
    def handler(request):
        cursor = request.url.params["cursorMark"]
        item = {
            "source": "MED",
            "id": "1" if cursor == "*" else "2",
            "title": "SYNTHETIC",
        }
        return httpx.Response(
            200,
            json={
                "hitCount": 2,
                "resultList": {"result": [item]},
                "nextCursorMark": "second" if cursor == "*" else "end",
            },
        )

    mocked_client(monkeypatch, handler)
    request = FetchRequest(source_id="europe_pmc", query="SYNTHETIC", max_records=1)
    first = read_public(request)
    assert first["next_continuation"]["read_count"] == 1 and not first["error"]
    second = read_public(
        FetchRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "continuation": first["next_continuation"],
            }
        )
    )
    assert (
        second["upstream_pagination_exhausted"] and second["next_continuation"] is None
    )
    assert second["records"][0].source_record_id == "MED:2"


def test_who_keyset_does_not_skip_when_previous_notice_moves_out_of_fixed_window(
    monkeypatch,
):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        query = request.url.params.get("$filter", "")
        assert "$skip" not in request.url.params
        if calls == 1:
            values = [who_value(i) for i in (1, 2, 3)]
        else:
            # Notice1 was updated after fixed until and vanished from old window.
            # Keyset continues after notice2; an offset2 would incorrectly start at notice4.
            assert "Id gt 00000000-0000-4000-8000-000000000002" in query
            values = [who_value(i) for i in (3, 4)]
        return httpx.Response(200, json={"value": values})

    mocked_client(monkeypatch, handler)
    req = FetchRequest(
        source_id="who_don",
        query="*",
        max_records=2,
        max_pages=1,
        updated_since="2026-10-01T00:00:00Z",
        updated_until="2026-10-18T00:00:00Z",
    )
    first = read_public(req)
    second = read_public(
        FetchRequest.model_validate(
            {**req.model_dump(mode="json"), "continuation": first["next_continuation"]}
        )
    )
    assert [row.source_record_id for row in second["records"]] == [
        who_value(3)["Id"],
        who_value(4)["Id"],
    ]
