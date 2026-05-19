"""LLM 抽取触发接口"""
from fastapi import APIRouter, HTTPException
from loguru import logger

from api.schemas.request import ExtractionTriggerRequest
from api.schemas.response import (
    ExtractionTriggerResponse, ExtractionStatusResponse, ExtractionStatsResponse,
)
from db.session import get_session
from db.models import Document

router = APIRouter(prefix="/api/extraction", tags=["extraction"])


@router.post("/trigger", response_model=ExtractionTriggerResponse)
def trigger_extraction(body: ExtractionTriggerRequest):
    """异步触发 LLM 抽取，立即返回，不等待完成。"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        if body.uuids:
            docs = session.query(Document).filter(
                Document.uuid.in_(body.uuids),
                Document.is_deleted == False,
            ).all()
        else:
            docs = session.query(Document).filter(
                Document.extraction_status == "pending",
                Document.is_deleted == False,
            ).limit(body.limit).all()

        count = 0
        for doc in docs:
            doc.extraction_status = "processing"
            count += 1

        logger.info(f"[Extraction] 触发抽取: {count} 篇")

    # 异步触发：在后台线程执行，不阻塞 API 响应
    if count > 0:
        import threading
        from flows.extraction_flow import run_extraction_for_uuids, run_extraction_batch
        uuids_to_run = [d.uuid for d in docs]
        t = threading.Thread(
            target=run_extraction_for_uuids,
            args=(uuids_to_run,),
            daemon=True,
        )
        t.start()

    return ExtractionTriggerResponse(
        triggered=count,
        message=f"已触发 {count} 篇文档的 LLM 抽取任务",
    )


@router.get("/stats", response_model=ExtractionStatsResponse)
def get_extraction_stats():
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        def _count(status: str) -> int:
            return session.query(Document).filter(
                Document.extraction_status == status,
                Document.is_deleted == False,
            ).count()

        return ExtractionStatsResponse(
            pending=_count("pending"),
            processing=_count("processing"),
            done=_count("done"),
            failed=_count("failed"),
        )


@router.get("/status/{uuid}", response_model=ExtractionStatusResponse)
def get_extraction_status(uuid: str):
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        doc = session.query(Document).filter(
            Document.uuid == uuid, Document.is_deleted == False
        ).first()
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")

        return ExtractionStatusResponse(
            uuid=doc.uuid,
            extraction_status=doc.extraction_status,
            extraction_model=doc.extraction_model or "",
            extracted_at=doc.extracted_at.isoformat() if doc.extracted_at else None,
            extraction_error=doc.extraction_error or "",
        )
