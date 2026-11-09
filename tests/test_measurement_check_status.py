"""A reading is an object whatever its check says: the check is carried on the object (check.status), it does not
decide whether there is one. No database: the pure parts of db.measurements (what is a placeholder, what an answer
sets). The chain with a database is in tests/integration_measures.py."""

from datetime import datetime, timezone

import pytest

from contracts import data_objects as do
from db import measurements

SHA, VID = "e" * 64, "a" * 64
SEEN = datetime(2026, 11, 1, tzinfo=timezone.utc)
VERSION = {"source_id": "who_don", "source_record_id": "2026-DON512", "first_seen_at": SEEN, "proof_available_at": None}
WAITING = {"checker_version": measurements.NOT_CHECKED, "contract_version": "extraction_consistency.v3", "run_id": "r-1"}
CHECKED = {"checker_version": "biocheck.extraction_rules.v17", "contract_version": "extraction_consistency.v3",
           "run_id": "r-1"}


def reading(**final):
    """A reading as the database holds it: no unit written in the text (the usual D1 case), a stated period."""
    return {
        "final": {"object": "Rwanda", "metric": "confirmed cases", "value": "66", "unit": "", "scope": "",
                  "period": {"type": "point", "precision": "day", "value": "2026-10-28"}, "denominator": None,
                  "scope_mode": "object_metric_total", "measure_kind": "cumulative", "qualifier": "exact",
                  "modality": "actual", "dims_unlocated": [], **final},
        "item": {"quote": "As of 28 October 2026, 66 confirmed cases", "family": "case_count",
                 "dims": [{"kind": "disease", "value": "Marburg", "span": {"start": 3, "end": 9}}]},
        "measure": {"value_text": "66", "period_text": "28 October 2026"},
        "text_sha256": SHA, "version_id": VID, "quote_start": 0, "quote_end": 41, "model_config_sha256": "b" * 64,
        "text_processor_version": "datafabric.clean_text.v12"}


def test_an_unchecked_reading_without_a_unit_is_an_object_that_says_so():
    assert measurements._gate(reading()) is None  # no "not_consistent": the check is not asked
    body = measurements.body_of(reading(), None, VERSION, WAITING)
    do.MeasurementV2.model_validate(body)  # what is served is the contract
    assert body["check"] == {**WAITING, "status": "unchecked"} and body["status"]["state"] == "ok"
    assert body["unit"] == "unknown" and body["value_text"] == "66"  # nothing made up: the unit is unknown
    assert [d["check"] for d in body["dims"]] == ["unchecked"]


@pytest.mark.parametrize("status, dim", [("inconsistent", "mismatch"), ("consistent", "match"), ("unchecked", "unsupported")])
def test_the_answer_of_the_check_sets_the_check_status_and_the_dimensions(status, dim):
    answer = {"status": status, "dims": {"disease": {"result": dim}}}
    body = measurements.body_of(reading(), answer, VERSION, CHECKED)
    assert body["check"]["status"] == status and body["check"]["checker_version"] == CHECKED["checker_version"]
    assert [d["check"] for d in body["dims"]] == [dim]
    assert body["status"]["state"] == "ok"  # the life of the object is another thing than what its check found


@pytest.mark.parametrize("change, why", [
    ({"period": None}, "period_not_stated"),  # no period, no time axis: nothing is made up
    ({"qualifier": "approximate"}, "approximate_or_not_actual"),
    ({"modality": "planned"}, "approximate_or_not_actual"),
])
def test_what_is_still_no_object(change, why):
    assert measurements._gate(reading(**change)) == why
