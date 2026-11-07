"""processing.measure_rules against BioRadar's own rules (measure.v10 at b452279) on the same cleaned WHO DON texts
and the same model outputs: windows, contexts, quote placement and the strict request body of every (window,
measure) pair, field by field (tests/fixtures/measure_golden.json, made by tests/fixtures/measure_golden_make.py)."""

import json
from pathlib import Path

import pytest

from processing import measure_extract as mx
from processing import measure_rules as rules

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "measure_golden.json").read_text(encoding="utf-8"))
RADAR_PRODUCER = {"name": "bioradar.triage", "version": "measure.v10"}


def plain(value):
    return json.loads(json.dumps(value))


def subject(case):
    return {"source_id": "who_don", "record_id": case["record_id"], "version_id": "a" * 64, "raw_sha256": "b" * 64,
            "is_demo": False,
            "artifact": {"processor_version": "datafabric.clean_text.v12", "text_sha256": case["text_sha256"]}}


def fabric_result(case, window, measure):
    """What Fabric does with one measure of one window, in the golden's shape."""
    text = case["text"]
    contexts = rules.contexts_of(text, window[0])
    sent = rules.Sent(tuple(window), {k: v for k, v in contexts.items() if v})
    span, why = rules.place_quote(text, measure["quote"], tuple(window))
    if span is None:
        return {"reason": why}
    cited, why = rules.cited_sources(measure, sent)
    if cited is None:
        return {"quote": list(span), "reason": why}
    found, failed = rules.cited_dim_sources(measure, sent)
    built, why = rules.build(rules.Bound(subject(case), span[0], span[1], measure, RADAR_PRODUCER), text,
                             {**found, **cited}, failed)
    if built is None:
        return {"quote": list(span), "reason": why}
    return {"quote": list(span), "body": built[0], "final": built[1]}


def test_the_golden_covers_built_and_refused_measures():
    results = [r for c in GOLDEN["cases"] for w in c["windows"] for r in w["results"]]
    assert sum("body" in r for r in results) >= 4
    reasons = {r.get("reason") for r in results}
    assert {"quote_not_in_window", "quote_not_in_text", "value_not_a_supported_number",
            "measure_kind_other_is_not_checkable", "period_literal_not_the_whole_period"} <= reasons
    contexts = {k for c in GOLDEN["cases"] for w in c["windows"] for k, v in w["contexts"].items() if v}
    assert contexts == {"heading", "table_header", "table_title"}


def test_the_model_output_schema_is_bioradars():
    assert [mx.Measure.model_validate(m).model_dump() for m in GOLDEN["inputs"]] == GOLDEN["measures"]


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[c["name"] for c in GOLDEN["cases"]])
def test_windows_and_contexts_are_bioradars(case):
    assert [list(w) for w in rules.windows_of(case["text"], GOLDEN["overlap"])] == [w["window"] for w in case["windows"]]
    for w in case["windows"]:
        got = rules.contexts_of(case["text"], w["window"][0])
        assert {k: ([list(v)] if v else None) for k, v in got.items()} == w["contexts"]


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[c["name"] for c in GOLDEN["cases"]])
def test_every_request_body_and_final_value_is_bioradars(case):
    for w in case["windows"]:
        for measure, expected in zip(GOLDEN["measures"], w["results"]):
            assert plain(fabric_result(case, w["window"], measure)) == expected


def test_a_literal_is_never_guessed():
    text = "As of 1 May 2026, 12 cases and 12 deaths were reported."
    m = mx.Measure.model_validate({"metric_text": "cases", "value_text": "12", "kind": "cumulative",
                                   "quote": text}).model_dump()
    span, _ = rules.place_quote(text, m["quote"], (0, len(text)))
    built, why = rules.build(rules.Bound({"raw_sha256": "b" * 64}, *span, m, RADAR_PRODUCER), text, {}, {})
    assert built is None and why == "value_literal_not_located"  # "12" twice: ambiguous, not chosen
