"""审核前核对固定原文；位置匹配不等于内容在专业上属实。"""
import json
import os
import re
from datetime import datetime, timezone

from sqlalchemy import text

from store.history_models import HistoryError, HistoryRecord


def validate_object_evidence(evidence: dict, *, source_doc_uuid: str, source_site: str) -> dict:
    from store import history

    value = dict(evidence)
    nested = value.get("source_quote")
    locator = dict(value.get("locator") or {})
    if isinstance(nested, dict):
        locator = {**nested, **locator}
    version = value.get("version_id") or value.get("source_version_id")
    quote = value.get("quote") or (nested if isinstance(nested, str) else locator.get("quote"))
    start = value.get("char_start", value.get("source_char_start", locator.get("start")))
    end = value.get("char_end", value.get("source_char_end", locator.get("end")))
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
        raise ValueError("审核需要明确的固定原文版本")
    if (not isinstance(quote, str) or not quote or type(start) is not int or type(end) is not int
            or start < 0 or end <= start or end - start != len(quote)):
        raise ValueError("审核需要精确原文和Unicode字符位置")
    space = locator.get("field", "clean_text")
    if space not in {"clean_text", "raw_content"}:
        raise ValueError("原文位置只能指向clean_text或raw_content")
    if locator.get("offset_unit", "unicode_codepoint") != "unicode_codepoint":
        raise ValueError("原文位置必须使用Unicode字符下标")

    try:
        with history.history_engine().begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout='3s'"))
            payload = conn.execute(
                text("SELECT payload FROM history_versions WHERE version_id=:version"),
                {"version": version},
            ).scalar_one_or_none()
        if payload is None:
            raise HistoryError("object_evidence_missing", "固定原文版本尚未发表", 409)
        candidate = HistoryRecord.model_validate(payload)
        for key in ("source_id", "source_record_id", "raw_sha256", "is_demo"):
            if key in value and value[key] is not None and value[key] != getattr(candidate, key):
                raise ValueError("证据标识与固定版本不一致")
        demo_config = os.environ.get("DATAFABRIC_PUBLICATION_IS_DEMO", "false")
        if demo_config not in {"true", "false"}:
            raise ValueError("对象来源空间配置无效")
        if candidate.is_demo != (demo_config == "true"):
            raise ValueError("不能跨真实/演示空间确认对象")
        record = history.read_version(
            version, candidate.source_id, candidate.source_record_id, candidate.is_demo,
            "current", datetime.now(timezone.utc),
        )
    except (HistoryError, ValueError):
        raise
    except Exception:
        raise HistoryError("object_evidence_unavailable", "固定原文暂不可核对", 503) from None

    if record.record_kind == "graph":
        raise ValueError("图包不能代替对象的原始文档")
    original_uuid = record.facts.get("origin_doc_uuid") or record.source_record_id
    original_site = record.facts.get("source_site") or record.source_id
    if original_uuid != source_doc_uuid or original_site != source_site:
        raise ValueError("固定版本不属于被审核对象的来源文档")

    content = record.clean_text
    if space == "raw_content":
        content = record.raw_content
        if record.source_id == "legacy_documents":
            raw = json.loads(record.raw_content)
            content = raw.get("original_raw_content")
            if not isinstance(content, str):
                raise ValueError("固定版本未保留被引用的原始文档字段")
            # 先从外层原件取出此字符串，再按Unicode下标定位；不是外层JSON的偏移。
            locator["container_json_pointer"] = "/original_raw_content"
    if end > len(content) or content[start:end] != quote:
        raise ValueError("引文与固定版本原文不一致")
    return {
        **value, "source_id": record.source_id, "source_record_id": record.source_record_id,
        "version_id": version, "raw_sha256": record.raw_sha256, "is_demo": record.is_demo,
        "source_doc_uuid": source_doc_uuid, "source_site": source_site,
        "quote": quote, "char_start": start, "char_end": end,
        "locator": {**locator, "field": space, "start": start, "end": end,
                    "offset_unit": "unicode_codepoint"},
    }
