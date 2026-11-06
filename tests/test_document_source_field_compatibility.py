"""来源原值及定位交接；字段存在不等于数值口径或公司身份已经核实。"""

import json

import pytest

from biz_rules.history_models import ArchiveRecord
from biz_rules.history_sources import project_record


def document(**fields):
    raw = {"record_kind": "document", "title": "SYNTHETIC 来源公告",
           "full_text": "来源原文，所有口径仍需下游核对。", **fields}
    return ArchiveRecord(source_record_id="SYNTHETIC-company-quarter",
                         record_kind="document",
                         raw_content=json.dumps(raw, ensure_ascii=False))


def test_source_numeric_context_and_license_survive_with_exact_native_pointers():
    fields = {
        "company_name": " 甲公司（来源原称）",
        "period_start": "2026-07-01",
        "period_end": "2026-10-01",
        "unit": "小时",
        "metric_role": "已用工时（来源写法）",
        "value": "80.0",
        "license": "SYNTHETIC research use only; not a verified license",
        "license_url": "https://example.invalid/source-license",
    }
    item = document(**fields)
    projected = project_record("external_archive", item)
    assert projected["facts"] == fields
    source = json.loads(item.raw_content)
    for key, expected in fields.items():
        assert projected["field_locators"][key] == {"json_pointer": "/" + key}
        assert source[key] == expected
    # 来源原称和指标角色不会生成确认身份、可比数值、许可审核或过去可见证明。
    evidence = projected["document_evidence"]
    assert evidence["statement_status"] == "source_reported"
    assert evidence["source_independence"]["status"] == "unknown"
    assert "schema_version" not in projected["facts"]
    assert "financial_observations" not in projected["facts"]
    assert "mapping_review" not in projected
    assert "availability_proof" not in projected


@pytest.mark.parametrize("value", [None, 0, "0"])
def test_source_value_keeps_explicit_unknown_and_zero_without_filling_context(value):
    projected = project_record("external_archive", document(value=value))
    assert projected["facts"] == {"value": value}
    assert projected["field_locators"]["value"] == {"json_pointer": "/value"}
    for key in ("company_name", "period_start", "period_end", "unit", "metric_role",
                "license", "license_url"):
        assert key not in projected["facts"]
        assert key not in projected["field_locators"]


def test_missing_fields_remain_missing_and_period_labels_are_not_normalized():
    absent = project_record("external_archive", document())
    assert absent["facts"] == {}
    supplied = {"company_name": None, "period_start": "2026年第三季度",
                "period_end": None, "unit": None, "metric_role": None,
                "license": None, "license_url": None}
    projected = project_record("external_archive", document(**supplied))
    assert projected["facts"] == supplied
    assert "value" not in projected["facts"]
    assert projected["source_published_at"] is None
    assert projected["event_time"] is None
