"""
数据库写操作仓库层 —— 所有 DB 硬逻辑集中在此。
外部模块只调 upsert_document()，不感知 ORM 细节。
"""
from datetime import datetime

from loguru import logger

from biz_rules.schemas import MetadataEnvelope
from db.models import Document, Attachment
from sqlalchemy import delete
from db.session import get_session


def upsert_document(envelope: MetadataEnvelope) -> None:
    """
    将 MetadataEnvelope upsert 到 documents + attachments 表。
    DB 未配置时静默跳过。
    """
    with get_session() as session:
        if session is None:
            return

        crawl_ts = None
        raw_ts = envelope.provenance.crawl_timestamp
        if raw_ts:
            try:
                crawl_ts = datetime.fromisoformat(raw_ts)
            except ValueError:
                pass

        values = dict(
            title=envelope.title,
            publish_date=envelope.publish_date,
            category=envelope.category,
            summary=envelope.summary,
            drug_name=envelope.drug_name,
            approval_number=envelope.approval_number,
            manufacturer=envelope.manufacturer,
            keywords=envelope.keywords,
            source_url=envelope.provenance.source_url,
            source_site=envelope.provenance.source_site,
            local_file_path=envelope.provenance.local_file_path,
            parent_uuid=envelope.provenance.parent_uuid,
            crawl_timestamp=crawl_ts,
            confidence_level=envelope.confidence.level,
            confidence_score=envelope.confidence.score,
            confidence_reason=envelope.confidence.reason,
            parse_method=envelope.parse_method,
            markdown_path=envelope.markdown_path,
            raw_file_path=envelope.raw_file_path,
            status=envelope.status,
            manual_score=envelope.manual_score,
            reviewer_note=envelope.reviewer_note,
            minio_bucket=envelope.future_minio_bucket,
            minio_object_key=envelope.future_minio_object_key,
            doc_type=envelope.doc_type,
            language=envelope.language,
        )


        existing = session.get(Document, envelope.uuid)
        if existing:
            for k, v in values.items():
                setattr(existing, k, v)
            # 删旧附件，重建（保持幂等）
            session.execute(delete(Attachment).where(Attachment.parent_uuid == existing.uuid))
            session.flush()
            target_uuid = existing.uuid
        else:
            doc = Document(uuid=envelope.uuid, **values)
            session.add(doc)
            session.flush()
            target_uuid = doc.uuid

        for att in envelope.attachments:
            session.add(Attachment(
                parent_uuid=target_uuid,
                file_name=att.file_name,
                file_url=att.file_url,
                file_uuid=att.file_uuid,
            ))

    logger.info(f"[DB] upsert 完成: {envelope.uuid}")
