"""Which readings may stand for each other across a re-read (db.measurements): only the same full candidate."""

import copy

from db import measurements as m

ITEM = {"kind": "measurement", "producer": {"name": "datafabric.measure_extract", "version": "v1+a"},
        "locator": {"passage": "p1", "start": 27, "end": 71}, "quote": "截至2026年10月28日，共报告确诊病例66例",
        "family": "case_count",
        "fields": {"value": {"state": "stated", "value": "66", "span": {"passage": "p1", "start": 60, "end": 62}},
                   "metric": {"state": "stated", "value": "确诊病例", "span": {"passage": "p1", "start": 56, "end": 60}},
                   "unit": {"state": "stated", "value": "例", "span": {"passage": "p1", "start": 62, "end": 63}},
                   "scope_mode": "unknown", "measure_kind": "cumulative", "qualifier": "exact", "modality": "actual"},
        "dims": [{"kind": "disease", "state": "stated", "value": "马尔堡病毒病",
                  "span": {"passage": "p1", "start": 50, "end": 56}}]}


def changed(**edit):
    item = copy.deepcopy(ITEM)
    for path, value in edit.items():
        node = item
        *head, last = path.split("__")
        for key in head:
            node = node[key]
        node[last] = value
    return item


def test_positions_and_the_producer_do_not_make_another_candidate():
    moved = changed(locator={"passage": "p1", "start": 127, "end": 171},
                    fields__value__span={"passage": "c1", "start": 160, "end": 162},
                    producer={"name": "datafabric.measure_extract", "version": "v1+b"})
    assert m._match_key(moved) == m._match_key(ITEM)


def test_another_unit_dimension_or_kind_is_another_candidate():
    for other in (changed(fields__unit={"state": "unknown"}), changed(dims=[]),
                  changed(fields__measure_kind="stock"), changed(family="")):
        assert m._match_key(other) != m._match_key(ITEM)  # the same quote, value and metric are not enough


def test_an_ambiguous_candidate_supersedes_nothing():
    rows = [{"item": ITEM, "id": 1}, {"item": changed(locator={"passage": "p1", "start": 1, "end": 2}), "id": 2},
            {"item": changed(dims=[]), "id": 3}]
    unique = m._unique(rows)
    assert [r["id"] for r in unique.values()] == [3]
