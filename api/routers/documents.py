# -*- coding: utf-8 -*-
"""文档查询接口"""

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from sqlalchemy import false
from sqlalchemy import func as sqlfunc

from api.schemas.response import (
    AttachmentItem,
    DocumentDetailResponse,
    DocumentItem,
    DocumentListResponse,
)
from graph.db.extraction_subjects import listed, subject_text
from graph.db.models import Attachment, DocExtra, Document
from graph.db.session import get_session
from store.history_models import HistoryError

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("/sites")
def get_source_sites():
    """返回 documents 表中所有去重后的 source_site，前端筛选下拉用。"""
    with get_session() as session:
        if session is None:
            return {"sites": []}
        rows = (
            session.query(Document.source_site)
            .filter(Document.is_deleted == false(), Document.source_site != "")
            .distinct()
            .order_by(Document.source_site)
            .all()
        )
        sites = [r[0] for r in rows if r[0]]
        return {"sites": sites}


@router.get("", response_model=DocumentListResponse)
def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    source_site: str = Query(""),
    doc_status: str = Query(""),
    extraction_status: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
):
    """文档列表查询（分页 + 多条件过滤）"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            # 只查列表需要的列，避免拉 raw_content 等大字段
            cols = (
                Document.id, Document.uuid, Document.title,
                Document.source_site, Document.publish_date, Document.doc_status,
                Document.confidence_score, Document.extraction_status,
                Document.weknora_sync_status,
                Document.updated_at,
            )
            q = (
                session.query(*cols)
                .filter(Document.is_deleted == false(), listed(Document))
            )

            # 动态筛选条件
            if source_site:
                q = q.filter(Document.source_site == source_site)
            if doc_status:
                q = q.filter(Document.doc_status == doc_status)
            if extraction_status:
                q = q.filter(Document.extraction_status == extraction_status)
            if date_from:
                q = q.filter(Document.publish_date >= date_from)
            if date_to:
                q = q.filter(Document.publish_date <= date_to)

            # 获取总数（用轻量 count，不走子查询）
            total = session.query(sqlfunc.count(Document.id)).filter(
                Document.is_deleted == false(),
                *([Document.source_site == source_site] if source_site else []),
                *([Document.doc_status == doc_status] if doc_status else []),
                *([Document.extraction_status == extraction_status] if extraction_status else []),
                *([Document.publish_date >= date_from] if date_from else []),
                *([Document.publish_date <= date_to] if date_to else []),
            ).scalar()

            # 分页查询
            rows = (
                q.order_by(Document.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )

            # 转换为响应模型
            items = [
                DocumentItem(
                    id=r.id,
                    uuid=r.uuid,
                    title=r.title or "",
                    source_site=r.source_site or "",
                    publish_date=r.publish_date or "",
                    doc_status=r.doc_status or "",
                    confidence_score=r.confidence_score or 0.0,
                    extraction_status=r.extraction_status or "",
                    weknora_sync_status=r.weknora_sync_status or "",
                    updated_at=r.updated_at.isoformat() if r.updated_at else None,
                )
                for r in rows
            ]

            logger.info(f"[API] 文档列表查询: page={page}, page_size={page_size}, total={total}")
            return DocumentListResponse(total=total, page=page, page_size=page_size, items=items)

        except Exception as e:
            logger.error(f"[API] 文档列表查询失败: {e}")
            raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.get("/{uuid}", response_model=DocumentDetailResponse)
def get_document(uuid: str):
    """文档详情查询（含全文、附件、扩展字段）"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            # 查询主文档
            doc = session.query(Document).filter(
                Document.uuid == uuid,
                Document.is_deleted == false()
            ).first()

            if doc is None:
                logger.warning(f"[API] 文档不存在: {uuid}")
                raise HTTPException(status_code=404, detail="文档不存在")

            # 查询扩展字段
            extra_row = session.query(DocExtra).filter(DocExtra.uuid == uuid).first()
            extra = extra_row.extra if extra_row else {}

            # 查询附件
            att_rows = session.query(Attachment).filter(
                Attachment.parent_uuid == uuid,
                Attachment.is_deleted == false()
            ).all()
            attachments = [
                AttachmentItem(
                    file_name=a.file_name or "",
                    file_url=a.file_url or "",
                    local_path=a.local_path or "",
                )
                for a in att_rows
            ]

            try:
                content = subject_text(doc)  # history 主体：先过权威访问检查
            except HistoryError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.code) from None

            # 构建响应
            response = DocumentDetailResponse(
                id=doc.id,
                uuid=doc.uuid,
                title=doc.title or "",
                raw_content=content,
                history_ref={"source_id": doc.history_source_id, "record_id": doc.history_record_id,
                             "version_id": doc.history_version_id, "text_processor_version": doc.history_text_version,
                             "text_sha256": doc.history_text_sha256} if doc.history_version_id else None,
                source_site=doc.source_site or "",
                publish_date=doc.publish_date or "",
                doc_status=doc.doc_status or "",
                confidence_score=doc.confidence_score or 0.0,
                parse_method=doc.parse_method or "",
                doc_type=doc.doc_type or "",
                language=doc.language or "",
                extra=extra,
                attachments=attachments,
                extraction_status=doc.extraction_status or "",
                extraction_model=doc.extraction_model or "",
                extraction_error=doc.extraction_error or "",
                extracted_at=doc.extracted_at.isoformat() if doc.extracted_at else None,
            )

            logger.info(f"[API] 文档详情查询: {uuid}")
            return response

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[API] 文档详情查询失败: {uuid} | {e}")
            raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")

