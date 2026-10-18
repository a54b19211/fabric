"""自动发表的原文/事实绑定反例，不调用真实来源。"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from biz_rules.collected_document import (
    CollectedDocument,
    collected_document,
    project_collected,
)
from biz_rules.history_models import ArchiveRecord, sha256

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def snapshot(uuid="SYNTHETIC-document", *, status="RECRUITING"):
    protocol = {
        "identificationModule": {
            "nctId": "NCT00000001",
            "briefTitle": "SYNTHETIC trial",
        },
        "descriptionModule": {"briefSummary": "SYNTHETIC narrative"},
        "statusModule": {
            "overallStatus": status,
            "lastUpdatePostDateStruct": {"date": "2026-10", "type": "ACTUAL"},
        },
        "designModule": {"phases": ["PHASE1"]},
    }
    raw = (
        "# SYNTHETIC trial\n\nSYNTHETIC narrative.\n\n## 原始数据\n**protocolSection**: "
        + json.dumps(protocol)
        + "\n"
    )
    return {
        "uuid": uuid,
        "source_site": "clinicaltrials",
        "title": "SYNTHETIC trial",
        "raw_content": raw,
        "source_url": "https://clinicaltrials.gov/study/NCT00000001",
        "publish_date": "mislabelled old date",
        "is_deleted": False,
    }


def test_collected_trial_uses_exact_frozen_protocol_not_legacy_date_and_strips_json_from_text():
    doc = collected_document(snapshot(), NOW, "insert")
    item = ArchiveRecord(
        source_record_id="legacy:clinicaltrials:SYNTHETIC-document",
        record_kind="trial",
        raw_content=doc.model_dump_json(),
    )
    result = project_collected(item, json.loads(item.raw_content))
    assert (
        result["record_kind"] == "trial"
        and result["facts"]["trial_id"] == "NCT00000001"
    )
    assert "protocolSection" not in result["clean_text"]
    assert result["source_published_at"] is None
    assert result["source_updated_at"]["precision"] == "month"
    assert "enrollment" not in result["facts"]
    assert result["field_locators"]["status"] == {
        "json_pointer": "/source_projection/protocolSection/statusModule/overallStatus"
    }


@pytest.mark.parametrize("change", ["value", "offset", "original"])
def test_collected_projection_cannot_substitute_typed_fields_or_raw_slice(change):
    value = collected_document(snapshot(), NOW, "insert").model_dump(mode="json")
    if change == "value":
        value["source_projection"]["protocolSection"]["statusModule"][
            "overallStatus"
        ] = "COMPLETED"
    elif change == "offset":
        value["protocol_locator"]["start"] += 1
    else:
        value["original_raw_content"] += " tampered"
        value["original_raw_sha256"] = sha256(value["original_raw_content"])
    if (
        change == "original"
    ):  # Appending outside the exact slice does not change its meaning.
        value["source_projection"]["protocolSection"]["statusModule"][
            "overallStatus"
        ] = "COMPLETED"
    with pytest.raises(ValidationError):
        CollectedDocument.model_validate(value)


def test_no_protocol_is_text_and_delete_is_explicit_tombstone_not_empty_success():
    value = snapshot()
    value["raw_content"] = "SYNTHETIC plain clinical note without original protocol."
    doc = collected_document(value, NOW, "backfill")
    assert doc.source_projection is None
    value["is_deleted"] = True
    tombstone = collected_document(value, NOW, "update")
    assert (
        tombstone.publication_state == "withdrawn"
        and not tombstone.original_raw_content
    )
    assert tombstone.previous_raw_sha256 == sha256(value["raw_content"])
    value["is_deleted"] = False
    value["raw_content"] = ""
    with pytest.raises(ValidationError):
        collected_document(value, NOW, "update")


def test_collected_old_wrappers_are_not_rewritten():
    from biz_rules.reviewed_graph import LegacyDocument

    raw = "SYNTHETIC original"
    old = LegacyDocument(
        source_doc_uuid="old",
        source_site="clinicaltrials",
        title="SYNTHETIC",
        original_raw_content=raw,
        original_raw_sha256=sha256(raw),
    )
    assert old.schema_version == "datafabric.legacy.document.v1"
    assert "publication_state" not in old.model_dump()


@pytest.mark.parametrize("backfill", [None, 0, 3])
def test_publisher_backfill_budget_is_separate_and_zero_keeps_consuming(
    monkeypatch, backfill
):
    import sys

    from flows import history_publish

    calls = []
    argv = ["history_publish", "--once", "--poll-seconds", "30", "--batch-size", "20"]
    if backfill is not None:
        argv.extend(["--backfill-batch-size", str(backfill)])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(history_publish, "publisher_heartbeat", lambda state: None)
    monkeypatch.setattr(
        history_publish,
        "backfill_once",
        lambda *, limit: calls.append(("backfill", limit)) or {"seeded": limit},
    )
    monkeypatch.setattr(
        history_publish,
        "publish_once",
        lambda: calls.append(("publish", None)) or {"state": "idle"},
    )
    history_publish.main()
    assert ("publish", None) in calls
    expected = (
        [] if backfill == 0 else [("backfill", 1 if backfill is None else backfill)]
    )
    assert [call for call in calls if call[0] == "backfill"] == expected
