# -*- coding: utf-8 -*-
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
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            # 查询待处理文档
            if body.uuids:
                # 验证 UUID 列表不为空
                if not body.uuids:
                    logger.warning("[API] 抽取触发: UUID 列表为空")
                    return ExtractionTriggerResponse(
                        triggered=0,
                        message="UUID 列表为空",
                    )
                
                docs = session.query(Document).filter(
                    Document.uuid.in_(body.uuids),
                    Document.is_deleted == False,
                ).all()
                
                # 检查是否存在不存在的 UUID
                found_uuids = {d.uuid for d in docs}
                not_found = set(body.uuids) - found_uuids
                if not_found:
                    logger.warning(f"[API] 抽取触发: 以下 UUID 不存在: {not_found}")
            else:
                docs = session.query(Document).filter(
                    Document.extraction_status == "pending",
                    Document.is_deleted == False,
                    Document.raw_content != "",
                ).limit(body.limit).all()

            count = 0
            skipped_empty = 0
            for doc in docs:
                if not doc.raw_content or not doc.raw_content.strip():
                    doc.extraction_status = "failed"
                    doc.extraction_error = "文档raw_content为空，无法抽取"
                    skipped_empty += 1
                    continue
                doc.extraction_status = "processing"
                count += 1

            if skipped_empty:
                logger.warning(f"[Extraction] 跳过 {skipped_empty} 篇 raw_content 为空的文档")

            logger.info(f"[Extraction] 标记 processing: {count} 篇")

        except Exception as e:
            logger.error(f"[Extraction] 查询文档失败: {e}")
            raise HTTPException(status_code=500, detail=f"查询文档失败: {str(e)}")

    # 异步执行抽取（不等待完成）
    if count > 0:
        try:
            from flows.extraction_flow import run_extraction_for_uuids
            uuids_to_run = [d.uuid for d in docs]
            # run_extraction_for_uuids 内部直接提交到 WorkerManager，无需额外线程
            run_extraction_for_uuids(uuids_to_run)
            logger.info(f"[Extraction] 已提交 {count} 篇到抽取队列")
        except Exception as e:
            logger.error(f"[Extraction] 提交抽取任务失败: {e}")
            # 不阻塞返回，只记录错误

    return ExtractionTriggerResponse(
        triggered=count,
        message=f"已提交 {count} 篇文档到抽取队列",
    )


@router.get("/stats", response_model=ExtractionStatsResponse)
def get_extraction_stats():
    """查询各抽取状态的文档数量。"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        def _count(status: str) -> int:
            return session.query(Document).filter(
                Document.extraction_status == status,
                Document.is_deleted == False,
            ).count()

        stats = ExtractionStatsResponse(
            pending=_count("pending"),
            processing=_count("processing"),
            done=_count("done"),
            failed=_count("failed"),
        )
        
        logger.info(f"[API] 抽取统计: {stats.json()}")
        return stats
        
    except Exception as e:
        logger.error(f"[API] 抽取统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")


@router.get("/workers", response_model=WorkerStatsResponse)
def get_worker_stats():
    """查询 WorkerManager 各 Worker 的队列状态。"""
    try:
        from flows.extraction_worker import get_extraction_manager
        manager = get_extraction_manager()
        
        # 获取 Worker 状态
        stats = manager.stats()
        workers = [
            WorkerItem(worker_id=w["worker_id"], queue_size=w["queue_size"])
            for w in stats
        ]
        
        response = WorkerStatsResponse(
            num_workers=manager.num_workers,
            total_pending=manager.total_pending(),
            workers=workers,
        )
        
        logger.info(f"[API] Worker 状态查询: workers={response.num_workers}, pending={response.total_pending}")
        return response
        
    except ImportError as e:
        logger.error(f"[API] WorkerManager 未初始化: {e}")
        return WorkerStatsResponse(
            num_workers=0,
            total_pending=0,
            workers=[],
        )
    except Exception as e:
        logger.error(f"[API] Worker 状态查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.get("/status/{uuid}", response_model=ExtractionStatusResponse)
def get_extraction_status(uuid: str):
    """查询单篇文档的抽取状态。"""
    # UUID 格式校验
    if not uuid or not uuid.strip():
        raise HTTPException(status_code=400, detail="uuid 不能为空")
    
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        doc = session.query(Document).filter(
            Document.uuid == uuid, 
            Document.is_deleted == False
        ).first()
        
        if doc is None:
            logger.warning(f"[API] 文档不存在: {uuid}")
            raise HTTPException(status_code=404, detail="文档不存在")

        response = ExtractionStatusResponse(
            uuid=doc.uuid,
            extraction_status=doc.extraction_status or "",
            extraction_model=doc.extraction_model or "",
            extracted_at=doc.extracted_at.isoformat() if doc.extracted_at else None,
            extraction_error=doc.extraction_error or "",
        )
        
        logger.info(f"[API] 抽取状态查询: {uuid} status={response.extraction_status}")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 抽取状态查询失败: {uuid} | {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")
