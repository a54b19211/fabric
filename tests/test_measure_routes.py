"""The two candidate routes of datafabric.measure_extract and an explicit scope (task book 01, 2.7): the rules route
writes the explicit counts of an English WHO DON notice as measures that take the same strict build; the routes agree
only on all nine items and never as independent evidence; a scope is a strict list."""

import json

import pytest

from clean import clean_text, measure_runs, processors
from clean import measure_agreement as agreement
from clean import measure_counts as counts
from clean import measure_extract as mx
from clean import measure_rules as rules

CUMULATIVE = "As of 28 October 2026, Rwanda has reported 66 cumulative confirmed cases of Marburg virus disease."
UNSTATED = "As of 12 October 2026, a total of 124 confirmed cases of cholera have been reported in Chad."
LONGER = "cumulative confirmed cases of Marburg virus disease"  # how a model may write the metric of CUMULATIVE
RAW ={"Id": "8c6e4a3e-2b0e-4b8e-9d55-1d1b1c7f5a09", "Title": "Marburg virus disease - Rwanda",
       "Summary": f"<p>{CUMULATIVE} {UNSTATED}</p>"}
ARTIFACT = clean_text.clean_who(json.dumps(RAW))
TEXT, SEGMENTS = ARTIFACT["text"], ARTIFACT["segments"]
SUBJECT = {"source_id": "who_don", "record_id": RAW["Id"], "version_id": "a" * 64, "raw_sha256": "b" * 64,
           "is_demo": True, "artifact": {"processor_version": clean_text.PROCESSOR_VERSION, "text_sha256": "c" * 64}}
KEYS = {"material": {}, "artifact": {}, "producer": {
    "name": mx.PRODUCER, "version": mx.PROCESSOR_VERSION, "model_config_sha256": None,
    "identity": mx.rules_identity(counts.METHOD)}}


def found(window=None):
    """(measures, skipped) the rules route finds in a window of the artifact (all of it by default)."""
    return counts.measures_of(TEXT, SEGMENTS, window or (0, len(TEXT)), RAW["Id"])


def built(measures):
    """(drafts, not built) as the processor makes the readings of a window from measures."""
    return mx.readings_of(measures, TEXT, rules.Sent((0, len(TEXT)), {}), SUBJECT, KEYS)


def reading_of(measure):
    """The reading a measure becomes, as the table keeps it: the strict request item without its caller's id and the
    final values."""
    (draft,), _ = built([measure])
    return {"reading_id": draft.reading_id, "item": {k: v for k, v in draft.body["items"][0].items() if k != "id"},
            "final": draft.final}


def test_the_rules_route_writes_a_stated_count_as_a_reading_and_lists_what_it_cannot():
    measures, skipped = found()
    assert skipped == [] and [m["value_text"] for m in measures] == ["66", "124"]
    drafts, not_built = built(measures)
    assert [d.final["value"] for d in drafts] == ["66"]
    reading = reading_of(measures[0])
    final = reading["final"]
    assert (final["metric"], final["unit"], final["scope"], final["measure_kind"]) == (
        "cumulative confirmed cases", "cases", "Rwanda", "cumulative")
    assert final["period"] == {"type": "point", "precision": "day", "value": "2026-10-28"}
    assert reading["item"]["producer"]["version"] == mx.rules_identity(counts.METHOD)
    # a count whose kind the notice does not state is listed with its reason, never guessed
    assert not_built == [{"measure": 1, "reason": "measure_kind_other_is_not_checkable"}]
    # the rules read only the sentences that lie wholly inside the window
    assert found((0, 40)) == ([], [])


def test_the_route_is_in_the_producer_identity_and_the_rules_read_a_declared_source():
    assert mx.route_of(mx.rules_identity(counts.METHOD)) == mx.RULES
    assert mx.route_of(mx.producer_identity("a" * 64)) == mx.MODEL
    assert measure_runs.Route(mx.RULES, KEYS["producer"], None).config == {"route": "rules", "method": counts.METHOD}
    assert set(counts.SOURCES) <= set(processors.MEASURE_SOURCES)


def test_the_routes_agree_only_when_all_nine_items_are_the_same_and_never_as_independent_evidence():
    measure = found()[0][0]
    rules_row = reading_of(measure)
    same = reading_of(dict(measure))
    other = reading_of(dict(measure, metric_text=LONGER))  # eight items the same, the metric not
    agree, conflict = agreement.compare(rules_row, same), agreement.compare(rules_row, other)
    assert agree["verdict"] == "agree" and all(item["same"] for item in agree["items"].values())
    assert conflict["verdict"] == "conflict"
    assert [name for name, item in conflict["items"].items() if not item["same"]] == ["metric"]
    result = agreement.agreement([rules_row], [other])
    assert result["independent_evidence"] is False
    assert result["pairs"][0]["reading_ids"] == {"rules": rules_row["reading_id"], "model": other["reading_id"]}
    assert result["only_rules"] == result["only_model"] == []  # both readings stay: neither route outranks the other


def test_a_reading_only_one_route_made_is_neither_agreement_nor_conflict():
    measure = found()[0][0]
    elsewhere = dict(measure, value_text="124", metric_text="confirmed cases", quote=UNSTATED, object_text="cholera",
                     period_text="As of 12 October 2026", period="2026-10-12", scope_text="Chad", dims=[])
    result = agreement.agreement([reading_of(measure)], [reading_of(elsewhere)])
    assert result["pairs"] == [] and len(result["only_rules"]) == len(result["only_model"]) == 1


def test_a_scope_is_a_strict_list_of_versions_and_their_windows():
    a, b = "a" * 64, "b" * 64
    scope = measure_runs.Scope.parse({"versions": [a, {"version_id": b, "windows": [3, 0]}]})
    assert scope.versions == {a: None, b: (0, 3)} and scope.selection(b)["windows"] == [0, 3]
    assert scope.sha256 == measure_runs.Scope.parse({"versions": [{"version_id": b, "windows": [0, 3]}, a]}).sha256
    for bad in ({}, {"versions": []}, {"versions": ["x"]}, {"versions": [a, a]},
                {"versions": [{"version_id": a, "windows": []}]}, {"versions": [{"version_id": a, "windows": [-1]}]},
                {"versions": [{"version_id": a, "windows": [1, 1]}]}):
        with pytest.raises(ValueError):
            measure_runs.Scope.parse(bad)
