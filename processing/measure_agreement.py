"""Whether the two candidate routes of datafabric.measure_extract (rules and model) agree about a reading (task book
01, 2.7).

Two readings are about the same count when they put their value at the same place of the artifact; the same place
and the same number are not enough to agree. They agree only when all nine items of ITEMS are the same, compared as
written (lower-cased, spaces folded - no synonym is guessed). Otherwise both readings stay and the pair is a
conflict: no route outranks the other and nothing is overwritten. Both routes read the same text, so even an
agreement is one reading made twice, never two independent confirmations.

The nine items are read from the reading's final values: condition is the modality with the date basis the
reading states; region_or_population is the scope with the population it states; suspected_or_confirmed is read
from the words of the metric.
"""

from __future__ import annotations

import re

from processing import measure_extract as mx

ITEMS = ("object", "metric", "unit", "period", "region_or_population", "cumulative_or_new", "suspected_or_confirmed",
         "condition", "qualifier")
STATUS_WORDS = (("suspected", r"suspected|probable|疑似"), ("confirmed", r"confirmed|确诊"))
NOTE = "both routes read the same text: their agreement is one reading made twice, not independent confirmation"


def _words(value):
    return " ".join(str(value or "").split()).casefold()


def _status(metric):
    found = [name for name, pattern in STATUS_WORDS if re.search(pattern, metric or "", re.IGNORECASE)]
    return "+".join(found) or "not_stated"


def items_of(reading) -> dict:
    """The nine items of a reading (a derived_measure_readings row: its strict request item and its final values)."""
    final = reading["final"]
    dims = {d["kind"]: _words(d["value"]) for d in reading["item"].get("dims") or []}
    return {
        "object": _words(final["object"]), "metric": _words(final["metric"]), "unit": _words(final["unit"]),
        "period": mx.canonical(final["period"]),
        "region_or_population": f"{_words(final['scope'])}|{dims.get('population', '')}",
        "cumulative_or_new": final["measure_kind"], "suspected_or_confirmed": _status(final["metric"]),
        "condition": f"{final['modality']}|{dims.get('date_basis', '')}", "qualifier": final["qualifier"],
    }


def compare(rules_reading, model_reading) -> dict:
    """One pair: each item of both readings and whether they are the same, and the verdict (agree only if all are)."""
    a, b = items_of(rules_reading), items_of(model_reading)
    items = {name: {"rules": a[name], "model": b[name], "same": a[name] == b[name]} for name in ITEMS}
    return {"verdict": "agree" if all(item["same"] for item in items.values()) else "conflict", "items": items,
            "reading_ids": {mx.RULES: rules_reading["reading_id"], mx.MODEL: model_reading["reading_id"]}}


def _place(reading):
    span = reading["item"]["fields"]["value"]["span"]
    return span["start"], span["end"]


def agreement(rules_readings, model_readings) -> dict:
    """The readings of the two routes of one artifact: every pair that puts its value at the same place with its
    verdict, and the readings only one route made (no pair, so neither agreement nor conflict)."""
    by_place = {}
    for reading in model_readings:
        by_place.setdefault(_place(reading), []).append(reading)
    pairs, paired = [], set()
    for reading in rules_readings:
        for other in by_place.get(_place(reading), []):
            pairs.append(compare(reading, other))
            paired.update((reading["reading_id"], other["reading_id"]))
    return {"independent_evidence": False, "note": NOTE, "pairs": pairs,
            "only_rules": [r["reading_id"] for r in rules_readings if r["reading_id"] not in paired],
            "only_model": [r["reading_id"] for r in model_readings if r["reading_id"] not in paired]}
