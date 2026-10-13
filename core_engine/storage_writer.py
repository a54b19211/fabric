"""
数据落盘引擎 —— 数据必须落库。
本地文件存储仅作为调试/开发辅助（ENABLE_LOCAL_STORAGE=True 时启用）。
"""

import json
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger
from prefect import task

from config.settings import APP
from config.crawler_settings import SITE_PROFILES
from biz_rules.schemas import (
    MetadataEnvelope,
    ProvenanceInfo,
    ConfidenceInfo,
    AttachmentInfo,
    validate_extra,
)
from biz_rules.document_validator import validate_document
from biz_rules.source_mapper import calculate_confidence
from db.repository import upsert_document
from core_engine.lineage_store import save_lineage


@task(name="write_to_database")
def write_to_database(parsed_result: dict) -> dict:
    """
    数据落库：将解析结果写入数据库。

    参数:
        parsed_result: 经 quality_checker 检验通过的解析结果

    返回:
        dict: {"uuid": ..., "status": ..., "confidence_score": ..., "success": bool}
    """
    doc_uuid = parsed_result["uuid"]
    site = parsed_result.get("source_site", "unknown") or "unknown"

    # 组装元数据信封
    structured = parsed_result.get("structured_data", {}) or {}
    sniffed = parsed_result.get("sniffed_meta", {}) or {}

    # 计算置信度
    confidence_result = calculate_confidence(
        source_url=parsed_result.get("source_url", ""),
        doi=sniffed.get("doi", ""),
        journal_name=sniffed.get("journal_name", ""),
    )

    # 判定审核状态
    status = "AUTO_PROCESSED" if confidence_result["score"] >= 0.8 else "PENDING_REVIEW"

    # 组装附件列表（同时保留 local_path）
    attachments = []
    for att in parsed_result.get("attachment_links", []):
        from core_engine.scrapling_client import make_uuid
        attachments.append(
            AttachmentInfo(
                file_name=att.get("name", ""),
                file_url=att.get("url", ""),
                file_uuid=make_uuid(att.get("url", "")),
                local_path=att.get("local_path", ""),
            )
        )

    # 来源特有字段：从 parsed_result.extra 或 structured_data 中取
    raw_extra = parsed_result.get("extra") or structured.get("extra") or {}
    validated_extra = validate_extra(site, raw_extra)

    envelope = MetadataEnvelope(
        uuid=doc_uuid,
        title=parsed_result.get("title") or structured.get("title", ""),
        publish_date=parsed_result.get("publish_date") or structured.get("publish_date", ""),
        category=structured.get("category", ""),
        summary=structured.get("summary", ""),
        raw_content=parsed_result.get("markdown", ""),
        provenance=ProvenanceInfo(
            source_url=parsed_result.get("source_url", ""),
            source_site=site,
            local_file_path=parsed_result.get("local_path", ""),
            parent_uuid=parsed_result.get("parent_uuid", ""),
            crawl_timestamp=parsed_result.get("timestamp", ""),
        ),
        lineage=parsed_result.get("lineage", {}),
        confidence=ConfidenceInfo(
            score=confidence_result["score"],
            reason=confidence_result["reason"],
        ),
        attachments=attachments,
        extra=validated_extra,
        parse_method=parsed_result.get("parse_method", ""),
        status=status,
        doc_type=parsed_result.get("doc_type", ""),
        language=parsed_result.get("language", ""),
    )

    # ── 字段级血缘落盘（data/lineage/<uuid>.json）──
    try:
        save_lineage(parsed_result)
    except Exception as e:
        logger.warning(f"[Storage] 血缘落盘失败（不影响主流程）: {doc_uuid} | {e}")

    # ── 文档级结构化校验（T5：落库前契约检查 + 违规计数，非阻断）──
    doc_report = validate_document(envelope.model_dump())
    if doc_report.violation_count > 0:
        logger.warning(
            f"[Storage] 文档级校验发现 {doc_report.violation_count} 处违规: {doc_uuid}"
        )
        for _v in doc_report.violations:
            logger.warning(f"  - {_v['field']} [{_v['rule']}]: {_v['message']}")

    # ── 核心：写入数据库 ──
    success = True
    try:
        upsert_document(envelope)
        logger.info(f"[Storage] 数据落库成功: {doc_uuid}")
    except Exception as e:
        logger.error(f"[Storage] 数据落库失败: {doc_uuid} | {e}")
        success = False

    # ── 可选：本地文件存储（开发/调试用）──
    if APP.ENABLE_LOCAL_STORAGE:
        try:
            _write_to_local_storage(parsed_result, envelope, doc_uuid, site)
        except Exception as e:
            logger.warning(f"[Storage] 本地文件写入失败（不影响主流程）: {doc_uuid} | {e}")

    return {
        "uuid": doc_uuid,
        "status": status,
        "confidence_score": confidence_result["score"],
        "success": success,
        "violation_count": doc_report.violation_count,
    }


def _write_to_local_storage(
    parsed_result: dict,
    envelope: MetadataEnvelope,
    doc_uuid: str,
    site: str,
) -> None:
    """
    可选：本地文件存储（仅用于开发/调试）。
    当 APP.ENABLE_LOCAL_STORAGE = True 时启用。
    """
    # Markdown 落盘
    md_dir = APP.MARKDOWN_DIR / site
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / f"{doc_uuid}.md"
    md_path.write_text(parsed_result.get("markdown", ""), encoding="utf-8")
    logger.debug(f"[Storage] Markdown 本地存储: {md_path}")

    # JSON 信封落盘
    json_dir = APP.JSON_DIR / site
    json_dir.mkdir(parents=True, exist_ok=True)
    json_path = json_dir / f"{doc_uuid}.json"
    json_path.write_text(
        envelope.model_dump_json(indent=2, by_alias=True),
        encoding="utf-8",
    )
    logger.debug(f"[Storage] JSON 本地存储: {json_path}")
