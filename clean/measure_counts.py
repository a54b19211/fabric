"""The rules route of datafabric.measure_extract (task book 01, 2.7): the explicit counts that who.explicit-count.v1
(fetch.who_facts) finds in the cleaned artifact of a WHO DON notice, written as the measures a model would write
for a reading window - so they take the same strict request build, scope rules, identity and BioCheck check.

The rules read the Summary and Overview of the notice; the artifact's own segments (their JSON pointer) say where
those are. Headings and tables are not read. A count the rules accept but that cannot be written as a checkable
measure here (the wording is not the metric's, no kind is stated) is not hidden: the request build or this module
lists it with its reason. The projection keeps its own copy of the same rules unchanged; nothing here is a second
source of facts, only a candidate generator beside the model. Both read the same text.
"""

from __future__ import annotations

from clean import measure_extract as mx
from fetch import who_facts

METHOD = who_facts.METHOD
SOURCES = ("who_don",)
NARRATIVE = ("paragraph", "list")  # the segment kinds that are sentences
WORDING = {"case_count": "case", "death_count": "death"}  # the unit word the text must use for each metric


def sections_of(text, segments):
    """{field: (section text, offset, narrative blocks)} of the fields the rules read: a section runs from the first to
    the last segment that points at it, and its blocks are the narrative segments (offsets inside the section)."""
    out = {}
    for field in who_facts.FIELDS:
        mine = [s for s in segments or [] if (s.get("source") or {}).get("json_pointer") == "/" + field]
        if mine:
            a, b = mine[0]["start"], mine[-1]["end"]
            out[field] = (text[a:b], a, [(s["start"] - a, s["end"] - a) for s in mine if s["kind"] in NARRATIVE])
    return out


def _once(quote, literal):
    """The literal when the sentence holds it exactly once, else "": the request build locates each literal in its
    quote and refuses one it finds twice, so an optional field that is not unique is left unstated instead."""
    return literal if literal and quote.count(literal) == 1 else ""


def _kind(statement):
    """The measure kind the statement states: cumulative, or new within a stated interval; nothing else is guessed."""
    kind, scope = statement["count_kind"], statement["time_scope"]["kind"]
    if kind == "cumulative":
        return "cumulative"
    if kind == "reported_new" or (kind == "report_in_period" and scope == "reporting_interval"):
        return "flow"
    return "other"  # the request build refuses it: a count without a stated kind is not checkable


def _period(statement, quote):
    """The period fields of a statement whose time is a date it is as of or a day-precise interval, else none."""
    scope = statement["time_scope"]
    raw = scope["raw"]
    if not raw or quote.count(raw) != 1:
        return {}
    if scope["kind"] == "as_of" and scope["as_of"]:
        return {"period_text": raw, "period": scope["as_of"], "period_type": "point"}
    if scope["kind"] == "reporting_interval" and scope["start"] and scope["end"]:
        return {"period_text": raw, "period": f"{scope['start']}/{scope['end']}", "period_type": "interval"}
    return {}


def _measure(text, statement):
    """(measure, None) or (None, reason): one counted statement as the measure a model would have written."""
    quote = statement["evidence"]["quote"]
    count = who_facts.COUNT.match(text, statement["value_evidence"]["start"])
    if count is None or not count["unit"].lower().startswith(WORDING[statement["metric"]]):
        return None, "unit_wording_differs_from_metric"  # e.g. "12 cases died" is a death count worded as cases
    disease, place = statement["disease"], statement["geography"]
    measure = {
        "object_text": _once(quote, disease), "metric_text": text[count.start("qualifiers"):count.end("unit")],
        "value_text": count["number"], "unit_text": count["unit"], **_period(statement, quote),
        "scope_text": _once(quote, place["text"] if place else ""), "kind": _kind(statement),
        "status": "confirmed" if statement["case_status"] == "confirmed" else "reported", "quote": quote,
        "family": "case_count" if statement["metric"] == "case_count" else "",
        "dims": [{"kind": "disease", "text": disease, "source": "quote"}] if disease else [],
    }
    return mx.Measure.model_validate(measure).model_dump(), None


def measures_of(text, segments, window, record_id):
    """(measures, skipped) of one reading window: the counted statements whose sentence lies wholly inside it, and
    those the rules accepted but this route cannot write as a measure, each with its reason. A statement cap hit
    inside the notice is listed too: the rest of the notice was not read."""
    found = who_facts.extract_counts(sections_of(text, segments), native_notice_id=record_id, clean_text=text)
    a, b = window
    measures, skipped = [], []
    for statement in found["statements"]:
        evidence = statement["evidence"]
        if not (a <= evidence["start"] and evidence["end"] <= b):
            continue
        measure, why = _measure(text, statement)
        if measure is None:
            skipped.append({"quote_start": evidence["start"], "reason": why})
        else:
            measures.append(measure)
    if found["truncated"]:
        skipped.append({"reason": "statement_cap_reached"})
    return measures, skipped
