"""固定观测契约/官方公告协议反例；HTTP替身不冒充线上采集。"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from fetch import rate
from fetch.observations import observation
from fetch.source_refresh import SourceWatch, _page_finished
from fetch.sources import project_record, read_public
from fetch.who_don import ENDPOINT, project_who
from fetch.windows import cycle_request
from store.history_models import ArchiveRecord, FetchRequest


def metric():
    return {
        "schema_version": "datafabric.observation.v1",
        "title": "SYNTHETIC revenue",
        "kind": "metric",
        "subject": {"namespace": "external_archive", "id": "COMPANY1"},
        "metric_id": "revenue",
        "metric_version": "quarterly-v1",
        "value": {"lower": "100", "upper": "100"},
        "unit": "USD",
        "population": "consolidated",
        "region": "Global",
        "period_start": "2020-01-01",
        "period_end": "2020-04-01",
        "measurement_basis": "reported",
        "status": "active",
        "basis": {"kind": "declared", "note": "SYNTHETIC teaching declaration"},
    }


@pytest.mark.parametrize(
    "change",
    [
        {"value": {"lower": True, "upper": "1"}},
        {"value": {"lower": "NaN", "upper": "100"}},
        {"value": {"lower": "invalid", "upper": "100"}},
        {"value": {"lower": "101", "upper": "100"}},
        {"period_end": "2019-12-31"},
        {"denominator": {"lower": "1", "upper": "2"}},
        {"kind": "invented"},
    ],
)
def test_observation_rejects_invalid_decimal_period_or_unknown_kind(change):
    with pytest.raises(ValidationError):
        observation({**metric(), **change})


def test_observation_projection_uses_existing_raw_keys_and_distinct_parser():
    data = metric()
    item = ArchiveRecord(
        source_record_id="M1", record_kind="document", raw_content=json.dumps(data)
    )
    result = project_record("external_archive", item)
    assert result["parser_version"] == "datafabric.observation.projection.v0.1"
    assert (
        result["facts"]["kind"] == "metric"
        and "observation_schema" not in result["facts"]
    )
    for key, locator in result["field_locators"].items():
        assert data[locator["json_pointer"][1:]] == result["facts"][key]


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
    with pytest.raises(ValidationError):
        FetchRequest.model_validate(
            {**second_request.model_dump(mode="json"), "query": "different scope"}
        )
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


def test_bound_pointer_is_rfc6901_and_decimal_precision_is_not_float_rounding():
    from fetch.observations import (
        native_json,
        native_pointer,
        validate_bound_values,
    )

    data = {**metric(), "value": {"lower": "1", "upper": "1"}}
    data["basis"] = {
        "kind": "source_bound",
        "ref": {
            "source_id": "external_archive",
            "source_record_id": "N",
            "version_id": "a" * 64,
            "raw_sha256": "b" * 64,
        },
        "field_pointers": {},
    }
    native = native_json('{"amount":1.0000000000000000001,"values":[1,2]}')
    assert str(native["amount"]) == "1.0000000000000000001"
    with pytest.raises(ValueError):
        native_pointer(native, "/amount/")
    with pytest.raises(ValueError):
        native_pointer(native, "/values/-1")
    assert native_pointer({"a/b": {"": 1}}, "/a~1b/") == 1
    # A value differing only beyond float precision must still fail source binding.
    base = observation({**metric(), "value": {"lower": "1", "upper": "1"}})
    expected = __import__(
        "fetch.observations", fromlist=["binding_values"]
    ).binding_values(base)
    expected.pop("subject.namespace")
    pointers = {}
    actual = {}
    for index, (key, value) in enumerate(expected.items()):
        pointers[key] = "/" + str(index)
        actual[str(index)] = value
    actual[str(list(expected).index("value.lower"))] = native["amount"]
    data["basis"]["field_pointers"] = pointers
    bound = observation(data)
    with pytest.raises(ValueError, match="不一致"):
        validate_bound_values(bound, actual)


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


def test_who_cycle_at_the_daily_ceiling_ends_with_the_reason_and_the_next_cycle_picks_up(monkeypatch, mid_hour_clock):
    sent = []
    mocked_client(monkeypatch, lambda request: sent.append(request) or httpx.Response(200, json={"value": [who_value(1)]}))
    for _ in range(200):  # the 200 requests of the day are used
        assert rate.take_for_url(ENDPOINT) is None
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    checkpoint = "2026-11-03T00:00:00+00:00"
    watch = SourceWatch.model_validate(
        {"id": "w", "cadence_seconds": 900, "request": {"source_id": "who_don", "query": "*", "max_records": 2}}
    )
    state = {"window_checkpoint": checkpoint}

    def cycle(moment):
        request, _, end = cycle_request(watch, state, moment)
        state.update(window_ended_at=end.isoformat(), request=request)
        batch = read_public(FetchRequest.model_validate(request))
        _page_finished(watch, state, {key: value for key, value in batch.items() if key != "records"}, moment)
        return batch

    refused = cycle(now)  # nothing was read, so there is no cursor: this used to end as source_watch_no_cursor
    assert refused["stop_reason"] == "outbound_quota_day" and refused["next_continuation"] is None and sent == []
    assert state["state"] == "idle" and "request" not in state and "last_success_at" not in state
    assert state["window_checkpoint"] == checkpoint and state["last_coverage"]["stop_reason"] == "outbound_quota_day"
    assert state["next_check_at"] == (now + timedelta(seconds=900)).isoformat()
    tomorrow = SimpleNamespace(time=lambda: 1_800_000_000 + 86400.0, monotonic=rate.time.monotonic, sleep=rate.time.sleep)
    monkeypatch.setattr(rate, "time", tomorrow)  # the ceiling counts per UTC day
    later = now + timedelta(days=1)
    read = cycle(later)  # the same checkpoint, the window reaches the new moment
    assert len(read["records"]) == 1 and len(sent) == 1
    assert state["window_checkpoint"] == later.isoformat() and state["last_success_at"] and "request" not in state
