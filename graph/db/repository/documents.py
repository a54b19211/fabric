"""Documents, their extra payload, authority resolutions, attachments and extraction status."""

import json
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import false

from graph.db.models import (
    Attachment,
    DocAuthorityResolution,
    DocExtra,
    Document,
)
from graph.db.repository.audit import _write_operation_log


def _new_document(envelope) -> Document:
    return Document(
        uuid=envelope.uuid,
        title=envelope.title or "",
        publish_date=envelope.publish_date or "",
        category=envelope.category or "",
        summary=envelope.summary or "",
        raw_content=envelope.raw_content or "",
        source_url=envelope.provenance.source_url or "",
        source_site=envelope.provenance.source_site or "",
        parent_uuid=envelope.provenance.parent_uuid or "",
        crawl_timestamp=_parse_ts(envelope.provenance.crawl_timestamp),
        confidence_score=envelope.confidence.score,
        confidence_reason=envelope.confidence.reason or "",
        parse_method=envelope.parse_method or "",
        doc_status=envelope.status or "AUTO_PROCESSED",
        reviewer_note=envelope.reviewer_note or "",
        doc_type=envelope.doc_type or "",
        language=envelope.language or "",
        lineage=json.dumps(envelope.lineage or {}, ensure_ascii=False),
        weknora_sync_status="pending",
        extraction_status="pending",
    )


def _update_mutable_fields(doc: Document, envelope) -> None:
    """只更新可变字段，保留 extraction_status / weknora_sync_status 等流水线状态"""
    doc.title = envelope.title or doc.title
    doc.summary = envelope.summary or doc.summary
    doc.raw_content = envelope.raw_content or doc.raw_content
    doc.source_url = envelope.provenance.source_url or doc.source_url
    doc.parse_method = envelope.parse_method or doc.parse_method
    doc.doc_type = envelope.doc_type or doc.doc_type
    doc.language = envelope.language or doc.language
    if envelope.lineage:
        doc.lineage = json.dumps(envelope.lineage, ensure_ascii=False)


def _replace_attachments(session, envelope) -> None:
    """先删后插，保证幂等"""
    session.query(Attachment).filter(
        Attachment.parent_uuid == envelope.uuid
    ).delete(synchronize_session=False)
    for att in envelope.attachments:
        session.add(Attachment(
            parent_uuid=envelope.uuid,
            file_name=att.file_name or "",
            file_url=att.file_url or "",
            file_uuid=att.file_uuid or "",
            local_path=getattr(att, "local_path", "") or "",
        ))


def upsert_document(envelope) -> dict | None:
    """
    幂等写入文档。同一 uuid 重复调用只更新，不新增。
    同时写入：documents + doc_extra（extra 非空时）+ attachments（有附件时）。
    参数 envelope 是 MetadataEnvelope 对象。
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            raise RuntimeError("数据库未配置，文档未保存")

        # ── 1. documents 表 ──────────────────────────────────────────
        doc = session.query(Document).filter(Document.uuid == envelope.uuid).with_for_update().first()
        if doc is None:
            doc = _new_document(envelope)
            session.add(doc)
            logger.info(f"[DB] 新增文档: {envelope.uuid}")
            is_new = True
        else:
            # 旧链路没有版本化发布：变化先拒写，不能沿用旧抽取/审核成果盖成新正文。
            incoming = envelope.raw_content or ""
            if doc.raw_content and incoming and doc.raw_content != incoming:
                from graph.publish.publication import enqueue_changed_document

                return enqueue_changed_document(session, envelope)
            _update_mutable_fields(doc, envelope)
            logger.info(f"[DB] 更新文档: {envelope.uuid}")
            is_new = False

        session.flush()  # 确保 doc.id 可用
        _write_operation_log(
            session, "insert" if is_new else "update", "documents", doc.id,
            business_action="upsert",
            description=f"{'新增' if is_new else '更新'}文档: {envelope.uuid} site={envelope.provenance.source_site or ''}",
            details={"uuid": envelope.uuid, "source_site": envelope.provenance.source_site or "",
                      "doc_type": envelope.doc_type or "", "parse_method": envelope.parse_method or ""},
            trigger_source="upsert_document",
        )

        # ── 2. doc_extra 表 ──────────────────────────────────────────
        if envelope.extra:
            _upsert_doc_extra_in_session(
                session,
                uuid=envelope.uuid,
                source_site=envelope.provenance.source_site or "",
                extra=envelope.extra,
            )

        # ── 3. attachments 表 ────────────────────────────────────────
        if envelope.attachments:
            _replace_attachments(session, envelope)

        from graph.publish.publication import enqueue_current_document

        enqueue_current_document(session, envelope.uuid)
        logger.info(f"[DB] 文档入库成功: {envelope.uuid}")


def upsert_doc_extra(uuid: str, source_site: str, extra: dict) -> None:
    """单独写入/更新 doc_extra 表。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        _upsert_doc_extra_in_session(session, uuid, source_site, extra)


def _upsert_doc_extra_in_session(session, uuid: str, source_site: str, extra: dict) -> None:
    """内部辅助：在已有 session 中写 doc_extra，不自行 commit。"""
    row = session.query(DocExtra).filter(DocExtra.uuid == uuid).first()
    if row is None:
        row = DocExtra(uuid=uuid, source_site=source_site, extra=extra)
        session.add(row)
        session.flush()
        _write_operation_log(
            session, "insert", "doc_extra", row.id,
            business_action="upsert",
            description=f"新增 doc_extra: uuid={uuid} site={source_site}",
            details={"uuid": uuid, "source_site": source_site},
            trigger_source="_upsert_doc_extra_in_session",
        )
    else:
        row.extra = extra
        row.source_site = source_site
        _write_operation_log(
            session, "update", "doc_extra", row.id,
            business_action="upsert",
            description=f"更新 doc_extra: uuid={uuid} site={source_site}",
            details={"uuid": uuid, "source_site": source_site},
            trigger_source="_upsert_doc_extra_in_session",
        )


def get_authority_resolutions(uuid: str) -> list[dict]:
    """按文档读取对齐结果明细（供统计/论文取数/前端展示）。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(DocAuthorityResolution)
            .filter(
                DocAuthorityResolution.uuid == uuid,
                DocAuthorityResolution.is_deleted.is_(False),
            )
            .order_by(DocAuthorityResolution.id_type, DocAuthorityResolution.id)
            .all()
        )
        return [
            {
                "id_type": r.id_type,
                "resolution_key": r.resolution_key,
                "found": r.found,
                "degraded": r.degraded,
                "skipped": r.skipped,
                "std_id": r.std_id,
                "source_id": r.source_id,
                "raw_value": r.raw_value,
                "resolved_value": r.resolved_value,
                "candidate": r.candidate,
                "rule": r.rule,
                "candidates_tried": r.candidates_tried,
                "confidence": r.confidence,
            }
            for r in rows
        ]


def get_document_by_uuid(uuid: str) -> Optional[Document]:
    """根据 UUID 获取文档（含软删除过滤）。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return None
        return (
            session.query(Document)
            .filter(Document.uuid == uuid, Document.is_deleted == false())
            .first()
        )


def update_extraction_status(
    uuid: str,
    status: str,
    model: str = "",
    error: str = "",
    extracted_at=None,
) -> None:
    """更新文档的 LLM 抽取状态。status: pending / queued / processing / done / failed"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        doc = session.query(Document).filter(Document.uuid == uuid).first()
        if doc is None:
            logger.warning(f"[DB] update_extraction_status: 文档不存在 {uuid}")
            return
        before_status = doc.extraction_status
        doc.extraction_status = status
        if model:
            doc.extraction_model = model
        if error:
            doc.extraction_error = error
        if status == "done":
            doc.extracted_at = extracted_at or datetime.now(timezone.utc)
        _write_operation_log(
            session, "update", "documents", doc.id,
            business_action="extract",
            description=f"extraction_status: {before_status} → {status} uuid={uuid}",
            details={"uuid": uuid, "extraction_status": {"before": before_status, "after": status}, "model": model, "error": error},
            trigger_source="update_extraction_status",
        )


def reset_stale_extractions(max_age_minutes: int = 0) -> int:
    """
    将残留的 queued / processing 重置为 pending。

    max_age_minutes:
      - 0（默认）：重置所有，启动时使用
      - > 0：只重置 updated_at 超过该时间的文档，运行时使用

    适用场景：Worker 超时/崩溃后，RQ 已丢弃任务但 DB 仍显示 queued/processing。
    """
    from datetime import datetime, timedelta, timezone

    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return 0
        q = session.query(Document).filter(
            Document.extraction_status.in_(["queued", "processing"]),
            Document.is_deleted == false(),
        )
        if max_age_minutes > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
            q = q.filter(Document.updated_at < cutoff)

        result = q.update(
            {"extraction_status": "pending", "extraction_error": "任务超时/服务重启，状态回退"},
            synchronize_session=False,
        )
        if result:
            logger.info(f"[DB] reset_stale_extractions(max_age={max_age_minutes}m): {result} 篇回退 pending")
        return result


def get_pending_extraction(limit: int = 100) -> list[dict]:
    """
    查询 extraction_status=pending 且 is_deleted=False 的文档。
    返回字段：uuid, title, source_site, raw_content, doc_type
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(
                Document.uuid,
                Document.title,
                Document.source_site,
                Document.raw_content,
                Document.doc_type,
            )
            .filter(
                Document.extraction_status == "pending",
                Document.is_deleted == false(),
            )
            .limit(limit)
            .all()
        )
        return [
            {
                "uuid": r.uuid,
                "title": r.title,
                "source_site": r.source_site,
                "raw_content": r.raw_content,
                "doc_type": r.doc_type,
            }
            for r in rows
        ]


def get_attachments(parent_uuid: str) -> list[Attachment]:
    """获取文档的附件列表（软删除过滤）。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        return (
            session.query(Attachment)
            .filter(
                Attachment.parent_uuid == parent_uuid,
                Attachment.is_deleted == false(),
            )
            .all()
        )


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """将 ISO 格式字符串解析为 datetime，失败返回 None。"""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None
