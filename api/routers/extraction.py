"""LLM 抽取触发接口"""
from fastapi import APIRouter, HTTPException
from loguru import logger

from api.schemas.request import ExtractionTriggerRequest
from api.schemas.response import (
    ExtractionTriggerResponse, ExtractionStatusResponse, ExtractionStatsResponse,
    WorkerStatsResponse, WorkerItem,
)
from db.session import get_session
from db.models import Document

router = APIRouter(prefix="/api/extraction", tags=["extraction"])


@router.post("/trigger", response_model=ExtractionTriggerResponse)
def trigger_extraction(body: ExtractionTriggerRequest):
    """
    触发 LLM 抽取，任务提交到 WorkerManager 后立即返回。
    各 Worker 异步并行处理，完成后回调更新状态。
    """
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

        logger.info(f"[Extraction] 标记 processing: {count} 篇")

    if count > 0:
        from flows.extraction_flow import run_extraction_for_uuids
        uuids_to_run = [d.uuid for d in docs]
        # run_extraction_for_uuids 内部直接提交到 WorkerManager，无需额外线程
        run_extraction_for_uuids(uuids_to_run)

    return ExtractionTriggerResponse(
        triggered=count,
        message=f"已提交 {count} 篇文档到抽取队列",
    )


@router.get("/stats", response_model=ExtractionStatsResponse)
def get_extraction_stats():
    """查询各抽取状态的文档数量。"""
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


@router.get("/workers", response_model=WorkerStatsResponse)
def get_worker_stats():
    """查询 WorkerManager 各 Worker 的队列状态。"""
    from flows.extraction_worker import get_extraction_manager
    manager = get_extraction_manager()
    workers = [
        WorkerItem(worker_id=w["worker_id"], queue_size=w["queue_size"])
        for w in manager.stats()
    ]
    return WorkerStatsResponse(
        num_workers=manager.num_workers,
        total_pending=manager.total_pending(),
        workers=workers,
    )


@router.get("/status/{uuid}", response_model=ExtractionStatusResponse)
def get_extraction_status(uuid: str):
    """查询单篇文档的抽取状态。"""
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
