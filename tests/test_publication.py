"""自动发表的原文/事实绑定反例，不调用真实来源。"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from graph.publish.collected_document import (
    CollectedDocument,
    collected_document,
    project_collected,
)
from store.record_models import ArchiveRecord, sha256

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
    from graph.publish.reviewed_graph import StoredDocument

    raw = "SYNTHETIC original"
    old = StoredDocument(
        source_doc_uuid="old",
        source_site="clinicaltrials",
        title="SYNTHETIC",
        original_raw_content=raw,
        original_raw_sha256=sha256(raw),
    )
    assert old.schema_version == "datafabric.legacy.document.v1"
    assert "publication_state" not in old.model_dump()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_real_nmpa_collector_headers_are_not_narrative_and_originals_stay_exact(
    newline,
):
    from pathlib import Path

    samples = json.loads(
        (
            Path(__file__).parent / "fixtures" / "collected_nmpa_frontmatter.json"
        ).read_text()
    )
    for sample in samples:
        raw = sample["raw_content"].replace("\n", newline)
        value = {
            "uuid": "FIXTURE-" + sample["source_doc_uuid"],
            "source_site": sample["source_site"],
            "title": sample["title"],
            "source_url": sample["source_url"],
            "raw_content": raw,
        }
        doc = collected_document(value, NOW, "insert")
        item = ArchiveRecord(
            source_record_id="legacy:nmpa:" + value["uuid"],
            record_kind="document",
            raw_content=doc.model_dump_json(),
        )
        projected = project_collected(item, json.loads(item.raw_content))
        end = raw.index(newline + "---" + newline) + len(newline + "---" + newline)
        assert projected["clean_text"] == raw[end:].strip("\r\n").rstrip()
        assert "publish_date:" not in projected["clean_text"]
        assert "source_site:" not in projected["clean_text"]
        assert sample["title"] in projected["clean_text"]
        assert doc.original_raw_content == raw and doc.original_raw_sha256 == sha256(
            raw
        )
        assert (
            projected["parser_version"]
            == "datafabric.collected.document.projection.v0.2"
        )


@pytest.mark.parametrize(
    "raw",
    [
        "---\nsource_site: nmpa\ndoc_type: notice\nlanguage: zh\n正文没有闭合模板",
        "---\nsource_site: other\ndoc_type: notice\nlanguage: zh\n---\n真实正文",
        "---\nsource_site: nmpa\ndoc_type: notice\nlanguage: zh\nunknown_business_field: 重要事实\n---\n正文",
        "真实正文\n---\npublish_date: 字段在正文中的说明，不能全局删词",
    ],
)
def test_unrecognized_metadata_or_body_is_not_deleted(raw):
    from graph.publish.collected_document import _narrative_text

    assert _narrative_text(raw, "nmpa") == raw


def test_only_real_html_tables_lose_markup_while_cell_text_and_biomedical_angles_survive():
    from graph.publish.collected_document import _narrative_text

    raw = (
        "SYNTHETIC A<B, CD4<CD8 and <anti-BCMA> stay in prose.\n"
        '<table border="1"><tr><td colspan="2"><p>有意义的单元格</p>'
        "<style>style_noise</style><script>script_noise</script></td>"
        '<td rowspan="2">下一单元格</td></tr></table>\n'
        "SYNTHETIC trailing paragraph."
    )
    clean = _narrative_text(raw, "nmpa")
    assert "A<B, CD4<CD8 and <anti-BCMA>" in clean
    assert "有意义的单元格" in clean and "下一单元格" in clean
    assert "SYNTHETIC trailing paragraph." in clean
    assert all(
        word not in clean
        for word in (
            "<table",
            "<td",
            "colspan",
            "rowspan",
            "style_noise",
            "script_noise",
        )
    )
    assert (
        _narrative_text("Example <table>not a cell</table>", "nmpa")
        == "Example <table>not a cell</table>"
    )


@pytest.mark.parametrize("unit", ["characters", "bytes"])
def test_input_size_diagnostics_have_only_safe_field_limit_and_unit(unit):
    from graph.publish.publication import _validation_error
    from store.record_models import MAX_RECORD_BYTES

    marker = "SYNTHETIC_VALUE_MUST_NOT_BE_RECORDED"
    raw = marker + (
        "x" * MAX_RECORD_BYTES
        if unit == "characters"
        else "药" * (MAX_RECORD_BYTES // 3 + 1)
    )
    with pytest.raises(ValidationError) as failure:
        ArchiveRecord(
            source_record_id="SYNTHETIC", record_kind="document", raw_content=raw
        )
    diagnostic = _validation_error(failure.value)
    assert set(diagnostic) == {"code", "message", "field", "limit", "unit"}
    assert diagnostic["code"] == "publication_record_too_large"
    assert diagnostic["field"] == "raw_content"
    assert diagnostic["limit"] == MAX_RECORD_BYTES and diagnostic["unit"] == unit
    assert marker not in json.dumps(diagnostic)
    assert "input_value" not in json.dumps(diagnostic)


def test_other_validation_does_not_copy_input_or_exception_message():
    from graph.publish.publication import _validation_error

    marker = "SYNTHETIC_INVALID_SOURCE_VALUE_MUST_NOT_BE_RECORDED"
    with pytest.raises(ValidationError) as failure:
        ArchiveRecord(
            source_record_id="bad\n" + marker, record_kind="document", raw_content="ok"
        )
    diagnostic = _validation_error(failure.value)
    assert set(diagnostic) == {"code", "message"}
    assert diagnostic["code"] == "publication_input_invalid"
    assert marker not in json.dumps(diagnostic)
