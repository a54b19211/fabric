"""Measurements without a database: a window's measure -> a reading (the hard floors) -> an object -> the receipt the
radar accepts (its series_sync._valid_receipt, copied below, and the keys of the shared contract fixture). The chain
on PostgreSQL is tests/integration_measures.py."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clean import measure_extract as mx
from clean import measure_runs, measurement_reads

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "measurement_feed_contract.json").read_text("utf-8"))
SEEN = datetime(2026, 11, 1, tzinfo=timezone.utc)
QUOTE = "截至2026年10月28日，卢旺达卫生部在卢旺达全国累计共报告马尔堡病毒病确诊病例66例"
TEXT = "# 马尔堡病毒病 - 卢旺达\n\n## 按报告日统计\n\n" + QUOTE + "。\n"
MEASURE = {"object_text": "卢旺达卫生部", "metric_text": "报告马尔堡病毒病确诊病例", "value_text": "66", "unit_text": "例",
           "period_text": "截至2026年10月28日", "period": "2026-10-28", "period_type": "point", "scope_text": "在卢旺达全国",
           "kind": "cumulative", "family": "case_count", "quote": QUOTE,
           "dims": [{"kind": "disease", "text": "马尔堡病毒病"}, {"kind": "date_basis", "text": "按报告日统计"}]}


def valid_receipt(r):  # BioRadar services/bioradar/research/series_sync.py _valid_receipt
    return (isinstance(r, dict) and isinstance(r.get("measurement_id"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", r["measurement_id"])) and isinstance(r.get("current"), bool)
            and isinstance(r.get("inputs"), list) and bool(r["inputs"]) and isinstance(r.get("identity"), dict)
            and isinstance(r.get("check"), dict) and isinstance(r.get("period"), dict)
            and isinstance(r.get("versions"), dict))


def test_windows_cover_the_text_overlap_and_end_at_a_line_or_sentence():
    text = "".join(f"第{n}句话说的是一件事。" * 20 + "\n" for n in range(10))
    spans = mx.windows_of(text)
    assert spans[0][0] == 0 and spans[-1][1] == len(text) and len(spans) > 1
    for (a, b), (c, d) in zip(spans, spans[1:]):
        assert a < c < b < d and b - c <= mx.OVERLAP  # each window repeats a little of the one before
        assert text[b - 1] in "\n。"  # never cut inside a number or a word


def test_a_measure_that_holds_the_floors_is_a_reading_with_its_places():
    reading, why = mx.reading_of(MEASURE, TEXT, (0, len(TEXT)))
    assert why is None and TEXT[reading["quote_start"]:reading["quote_end"]] == QUOTE
    assert (reading["value_text"], reading["unit"], reading["scope_mode"]) == ("66", "例", "stated")
    assert reading["period"] == {"type": "point", "precision": "day", "value": "2026-10-28", "start": None, "end": None}
    dims = {d["kind"]: TEXT[d["start"]:d["end"]] for d in reading["dims"]}
    assert dims == {"disease": "马尔堡病毒病", "date_basis": "按报告日统计"}  # the heading is placed in the window
    assert {d["check"] for d in reading["dims"]} == {"unchecked"}


@pytest.mark.parametrize("change, why", [
    ({"quote": QUOTE + "例"}, "quote_not_once_in_window"),
    ({"value_text": "6"}, "value_not_a_number_in_quote"),  # "6" of "66" is no number of its own
    ({"object_text": "马尔堡病毒病 - 卢旺达"}, None),  # a literal of the window, not of the quote: the route's reading
    ({"scope_text": "在刚果全国"}, "scope_text_not_in_window"),
    ({"period": "", "period_text": ""}, "period_not_stated_or_not_supported"),
    ({"kind": "other"}, "kind_not_stated"),
    ({"qualifier": "approximate"}, "approximate_or_not_actual"),
])
def test_what_is_no_reading(change, why):
    assert mx.reading_of({**MEASURE, **change}, TEXT, (0, len(TEXT)))[1] == why


def test_a_unit_the_text_does_not_write_is_unknown_and_the_reading_still_flows():
    reading, _ = mx.reading_of({**MEASURE, "unit_text": ""}, TEXT, (0, len(TEXT)))
    assert reading["unit"] == "unknown"


def test_the_rules_route_reads_an_explicit_who_count():
    quote = ("As of 28 October 2026, a cumulative total of 66 confirmed cases of Marburg virus disease have been "
             "reported in Rwanda.")
    text = "# Marburg virus disease - Rwanda\n\n" + quote + "\n"
    segments = [{"kind": "paragraph", "start": text.index(quote), "end": len(text) - 1,
                 "source": {"json_pointer": "/Summary"}}]
    measures, skipped = mx.rules_measures(text, segments, (0, len(text)), "rec-1")
    assert skipped == [] and len(measures) == 1
    reading, why = mx.reading_of(measures[0], text, (0, len(text)))
    assert why is None
    assert (reading["metric"], reading["value_text"], reading["unit"], reading["measure_kind"]) == (
        "confirmed cases", "66", "cases", "cumulative")
    assert (reading["object"], reading["scope"], reading["period"]["value"]) == ("Marburg virus disease", "Rwanda",
                                                                                 "2026-10-28")


def test_the_model_answer_is_read_as_the_schema_says_or_not_at_all():
    answer = json.dumps({"items": [{"index": 1, "measures": [MEASURE] * 4}]}, ensure_ascii=False)
    measures, why = mx.parse_output(answer)
    assert why is None and len(measures) == mx.MAX_MEASURES and measures[0]["metric_text"] == MEASURE["metric_text"]
    assert mx.parse_output("not json") == (None, "output_not_the_schema")
    assert mx.parse_output('{"items": [{"index": 2}]}') == (None, "no_item")


def row(w, reading, mid):
    """The object as derived_measurements holds it."""
    return {"measurement_id": mid, "reading_id": mid, "body": measure_runs.body_of(w, reading),
            "producer_identity": mx.RULES_IDENTITY, "checker_version": "unchecked", "contract_version": "unchecked",
            "text_processor_version": "datafabric.clean_text.v12"}


def created(seq, mid, at, kind="created", by=None):
    return {"kind": kind, "published_seq": seq, "measurement_id": mid, "produced_at": at, "effective_at": at,
            "material_visible_at": SEEN, "superseded_by": by}


def test_an_object_is_an_unchecked_receipt_the_radar_accepts_until_a_later_run_supersedes_it():
    w = {"run_id": "r" * 64, "version_id": "v" * 64, "source_id": "who_don", "source_record_id": "2026-DON512",
         "first_seen_at": SEEN, "proof_available_at": None}
    reading, _ = mx.reading_of(MEASURE, TEXT, (0, len(TEXT)))
    mid, later = "a" * 64, SEEN + timedelta(days=2)
    receipt = measurement_reads.receipt(row(w, reading, mid), [created(1, mid, SEEN)], "current", later)
    assert valid_receipt(receipt) and set(CONTRACT["pull"]["receipt_keys"]) <= set(receipt)
    assert receipt["current"] is True and receipt["state_at_cutoff"] == "ok"
    assert receipt["check"]["status"] == "unchecked" and receipt["versions"]["checker"] == "unchecked"
    assert set(receipt["versions"]) == set(CONTRACT["feed"]["version_keys"])
    assert receipt["identity"] == {"state": "provisional", "key": "literal:who_don|2026-DON512|卢旺达卫生部"}
    events = [created(1, mid, SEEN), created(2, mid, later, "superseded_by_processor", "b" * 64)]
    gone = measurement_reads.receipt(row(w, reading, mid), events, "current", later)
    assert gone["current"] is False and gone["not_current_reason"] == "superseded_by_processor"
    assert gone["status"] == {"state": "superseded", "reason": "superseded_by_processor", "superseded_by": "b" * 64}
    # what the system showed before the later run: replay at a cutoff before it sees the object current
    before = measurement_reads.receipt(row(w, reading, mid), events, "operational_replay", SEEN + timedelta(days=1))
    assert before["current"] is True and [e["kind"] for e in before["events_applied"]] == ["created"]
