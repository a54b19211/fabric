"""
三位一体落盘引擎 —— 原始文件 + Markdown + JSON 元数据信封 + 远程数据库推送。
"""

import json
import shutil
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
)
from biz_rules.source_mapper import calculate_confidence
from db.repository import upsert_document


def _minio_object_key(doc_uuid: str, file_type: str) -> str:
    """生成 MinIO 对象键（二期直接用）。"""
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    return f"{file_type}/{today}/{doc_uuid}"


@task(name="write_three_in_one")
def write_three_in_one(parsed_result: dict) -> dict:
    """
    三位一体落盘：将解析结果写入三个目录。

    1. /data/raw/{site}/{uuid}.* → 原始文件（如已由下载步骤保存，此处跳过）
    2. /data/markdown/{site}/{uuid}.md → Markdown 全文
    3. /data/json_envelopes/{site}/{uuid}.json → 元数据信封

    参数:
        parsed_result: 经 quality_checker 检验通过的解析结果

    返回:
        dict: {"uuid": ..., "md_path": ..., "json_path": ..., "raw_path": ...}
    """
    doc_uuid = parsed_result["uuid"]
    site = parsed_result.get("source_site", "unknown") or "unknown"

    # ---- 1. Markdown 落盘 ----
    md_dir = APP.MARKDOWN_DIR / site
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / f"{doc_uuid}.md"
    md_path.write_text(
        parsed_result.get("markdown", ""),
        encoding="utf-8",
    )
    logger.info(f"[Storage] Markdown 落盘: {md_path}")

    # ---- 2. 组装元数据信封 ----
    structured = parsed_result.get("structured_data", {})
    sniffed = parsed_result.get("sniffed_meta", {})

    # 计算置信度
    confidence_result = calculate_confidence(
        source_url=parsed_result.get("source_url", ""),
        doi=sniffed.get("doi", ""),
        journal_name=sniffed.get("journal_name", ""),
    )

    # 判定审核状态
    status = "AUTO_PROCESSED"
    if confidence_result["level"] == "C":
        status = "PENDING_MANUAL_REVIEW"

    # 组装附件列表
    attachments = []
    for att in parsed_result.get("attachment_links", []):
        from core_engine.scrapling_client import make_uuid
        attachments.append(
            AttachmentInfo(
                file_name=att.get("name", ""),
                file_url=att.get("url", ""),
                file_uuid=make_uuid(att.get("url", "")),
            )
        )

    # 确定原始文件相对路径
    raw_rel_path = ""
    local_path = parsed_result.get("local_path", "")
    if local_path:
        raw_rel_path = f"raw/{site}/{doc_uuid}.pdf"

    envelope = MetadataEnvelope(
        uuid=doc_uuid,
        title=parsed_result.get("title", structured.get("title", "")),
        publish_date=parsed_result.get("publish_date", structured.get("publish_date", "")),
        category=structured.get("category", ""),
        summary=structured.get("summary", ""),
        drug_name=structured.get("drug_name", ""),
        approval_number=structured.get("approval_number", ""),
        manufacturer=structured.get("manufacturer", ""),
        keywords=structured.get("keywords", []),
        provenance=ProvenanceInfo(
            source_url=parsed_result.get("source_url", ""),
            source_site=site,
            local_file_path=local_path,
            parent_uuid=parsed_result.get("parent_uuid", ""),
            crawl_timestamp=parsed_result.get("timestamp", ""),
        ),
        confidence=ConfidenceInfo(
            level=confidence_result["level"],
            score=confidence_result["score"],
            reason=confidence_result["reason"],
        ),
        attachments=attachments,
        parse_method=parsed_result.get("parse_method", ""),
        markdown_path=f"markdown/{site}/{doc_uuid}.md",
        raw_file_path=raw_rel_path,
        status=status,
        future_minio_bucket=APP.FUTURE_MINIO_BUCKET,
        future_minio_object_key=_minio_object_key(doc_uuid, "json_envelopes"),
        doc_type=parsed_result.get("doc_type", ""),
        language=parsed_result.get("language", ""),
    )

    # ---- 3. JSON 落盘 ----
    json_dir = APP.JSON_DIR / site
    json_dir.mkdir(parents=True, exist_ok=True)
    json_path = json_dir / f"{doc_uuid}.json"
    json_path.write_text(
        envelope.model_dump_json(indent=2, by_alias=True),
        encoding="utf-8",
    )
    logger.info(f"[Storage] JSON 信封落盘: {json_path}")

    # ---- 4. 推送到远程数据库 ----
    upsert_document(envelope)

    # 站点自定义 DB 写入（如 hkex_meta、pubmed_meta 等）
    db_writer = SITE_PROFILES.get(site, {}).get("db_writer")
    if db_writer:
        db_writer(doc_uuid, parsed_result)

    return {
        "uuid": doc_uuid,
        "md_path": str(md_path),
        "json_path": str(json_path),
        "raw_path": raw_rel_path,
        "status": status,
        "confidence_level": confidence_result["level"],
    }


