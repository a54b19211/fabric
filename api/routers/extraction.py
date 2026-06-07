# -*- coding: utf-8 -*-
"""LLM 抽取触发接口"""
from fastapi import APIRouter, HTTPException
from loguru import logger

from api.schemas.request import ExtractionTriggerRequest
from api.schemas.response import (
    ExtractionTriggerResponse, ExtractionStatusResponse, ExtractionStatsResponse,
    WorkerStatsResponse, WorkerItem, ReExtractResponse,
)
from db.repository import (
    is_db_connected, get_extraction_stats, get_pending_extraction_docs,
    update_extraction_status,
)

router = APIRouter(prefix="/api/extraction", tags=["extraction"])


@router.post("/trigger", response_model=ExtractionTriggerResponse)
def trigger_extraction(body: ExtractionTriggerRequest):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        if body.uuids:
            if not body.uuids:
                return ExtractionTriggerResponse(triggered=0, message="UUID 列表为空")
            docs = get_pending_extraction_docs(uuids=body.uuids)
            not_found = set(body.uuids) - {d["uuid"] for d in docs}
            if not_found:
                logger.warning(f"[API] 抽取触发: 以下 UUID 不存在: {not_found}")
        else:
            docs = get_pending_extraction_docs(limit=body.limit)

        count = 0
        skipped_empty = 0
        for doc in docs:
            if not doc.get("raw_content") or not doc["raw_content"].strip():
                update_extraction_status(doc["uuid"], "failed", error="文档raw_content为空，无法抽取")
                skipped_empty += 1
                continue
            update_extraction_status(doc["uuid"], "processing")
            count += 1

        if skipped_empty:
            logger.warning(f"[Extraction] 跳过 {skipped_empty} 篇 raw_content 为空的文档")
        logger.info(f"[Extraction] 标记 processing: {count} 篇")
    except Exception as e:
        logger.error(f"[Extraction] 查询文档失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询文档失败: {str(e)}")

    if count > 0:
        try:
            from flows.extraction_flow import run_extraction_for_uuids
            uuids_to_run = [d["uuid"] for d in docs if d.get("raw_content", "").strip()]
            run_extraction_for_uuids(uuids_to_run)
            logger.info(f"[Extraction] 已提交 {count} 篇到抽取队列")
        except Exception as e:
            logger.error(f"[Extraction] 提交抽取任务失败: {e}")

    return ExtractionTriggerResponse(triggered=count, message=f"已提交 {count} 篇文档到抽取队列")


@router.get("/stats", response_model=ExtractionStatsResponse)
def get_extraction_stats_api():
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    try:
        stats = get_extraction_stats()
        result = ExtractionStatsResponse(**stats)
        logger.info(f"[API] 抽取统计: {result.json()}")
        return result
    except Exception as e:
        logger.error(f"[API] 抽取统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.get("/status/{uuid}", response_model=ExtractionStatusResponse)
def get_extraction_status(uuid: str):
    from db.repository import get_document_by_uuid
    doc = get_document_by_uuid(uuid)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return ExtractionStatusResponse(
        uuid=doc.uuid,
        extraction_status=doc.extraction_status or "",
        extraction_model=doc.extraction_model or "",
        extracted_at=doc.extracted_at.isoformat() if doc.extracted_at else None,
        extraction_error=doc.extraction_error or "",
    )


@router.post("/re-extract/{uuid}", response_model=ReExtractResponse)
def re_extract_document(uuid: str):
    from db.repository import get_document_by_uuid, update_extraction_status
    doc = get_document_by_uuid(uuid)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    update_extraction_status(uuid, "pending")
    logger.info(f"[API] 重新抽取已设置: {uuid}")
    return ReExtractResponse(success=True, uuid=uuid, message="已重新加入抽取队列")


@router.get("/workers", response_model=WorkerStatsResponse)
def get_worker_stats():
    from flows.extraction_worker import get_extraction_manager
    mgr = get_extraction_manager()
    if mgr is None:
        return WorkerStatsResponse(num_workers=0, total_pending=0, workers=[])
    workers_list = [
        WorkerItem(worker_id=w.worker_id, queue_size=w.queue.qsize())
        for w in mgr._workers
    ]
    return WorkerStatsResponse(
        num_workers=len(workers_list),
        total_pending=sum(w.queue_size for w in workers_list),
        workers=workers_list,
    )
