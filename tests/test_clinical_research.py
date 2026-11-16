"""Existing-record research access; synthetic data and no service connections."""

import json

import pytest

from fetch.clinical_protocol import MAX_DOCUMENT_BYTES, project_clinical_trial

DOCUMENT_ID = "00000000-0000-4000-8000-000000000001"


def protocol():
    return {
        "identificationModule": {"nctId": "NCT00000001", "briefTitle": "Synthetic trial"},
        "descriptionModule": {"briefSummary": "A synthetic summary, not medical evidence."},
        "conditionsModule": {"conditions": ["Synthetic condition"]},
        "armsInterventionsModule": {
            "interventions": [
                {"type": "DRUG", "name": "Synthetic BCMA-001", "description": "not adopted as a name"},
            ]
        },
        "designModule": {"phases": ["PHASE1"]},
        "statusModule": {
            "overallStatus": "TERMINATED",
            "whyStopped": "Synthetic administrative reason",
            "startDateStruct": {"date": "2019-04", "type": "ESTIMATED"},
            "lastUpdatePostDateStruct": {"date": "2026-09-01", "type": "ACTUAL"},
        },
    }


def body(value=None):
    value = protocol() if value is None else value
    # The old prose is deliberately wrong; facts must come from the appendix.
    return (
        "# 教学合成资料\n开始日期：2026-09-01\n研究涉及药物/干预：wrong disease\n"
        "## 研究摘要\nA short old summary.\n---\n## 原始数据\n"
        "**protocolSection**: " + json.dumps(value, ensure_ascii=False) + "\n**hasResults**: True\n"
    )


def test_projection_uses_exact_unicode_source_slice_and_original_field_values():
    raw = body()
    projection = project_clinical_trial(raw)
    assert projection.status == "available"
    assert raw[projection.locator.start : projection.locator.end] == projection.text
    assert projection.locator.offset_unit == "unicode_codepoint"
    assert json.loads(projection.text) == protocol()
    assert projection.facts.interventions[0].name == "Synthetic BCMA-001"
    assert projection.facts.brief_summary != "A short old summary."
    assert projection.facts.start_date.model_dump() == {"date": "2019-04", "type": "ESTIMATED"}
    assert projection.facts.last_update_post_date.date == "2026-09-01"
    assert projection.field_paths["interventions"] == "/protocolSection/armsInterventionsModule/interventions"
    assert projection.field_paths["why_stopped"] == "/protocolSection/statusModule/whyStopped"
    assert "hasResults" not in projection.facts.model_dump()


def test_missing_fields_remain_unknown_and_real_empty_arrays_are_retained():
    value = {"identificationModule": {"nctId": "NCT00000002"}, "designModule": {"phases": []}}
    facts = project_clinical_trial(body(value)).facts
    assert facts.conditions is None
    assert facts.interventions is None
    assert facts.phases == []
    assert facts.why_stopped is None
    assert facts.start_date is None


@pytest.mark.parametrize(
    "raw",
    [
        "No source appendix",
        body() + "\n## 原始数据\n",
        body() + "\n**protocolSection**: {}\n",
        body().replace('"NCT00000001"', '"not-a-registry-id"'),
        body().replace(
            '"overallStatus": "TERMINATED"', '"overallStatus": "TERMINATED", "overallStatus": "COMPLETED"'
        ),
        body().replace('"name": "Synthetic BCMA-001"', '"name": 12'),
        body({**protocol(), "armsInterventionsModule": {"interventions": {}}}),
        body().replace('"briefSummary": "A synthetic summary, not medical evidence."', '"briefSummary": NaN'),
        body().replace("**protocolSection**: {", "**protocolSection**: broken {"),
        "x" * (MAX_DOCUMENT_BYTES + 1),
    ],
)
def test_unusable_or_ambiguous_sources_never_become_empty_successful_facts(raw):
    projection = project_clinical_trial(raw)
    assert projection.status == "unavailable"
    assert projection.reason
    assert projection.text is None and projection.facts is None and projection.locator is None
