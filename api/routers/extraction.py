# -*- coding: utf-8 -*-
"""LLM 抽取触发接口"""
from fastapi import APIRouter, HTTPException
from loguru import logger

from api.schemas.request import ExtractionTriggerRequest
from api.schemas.response import (
    ExtractionTriggerResponse, ExtractionStatusResponse, ExtractionStatsResponse,
    WorkerStatsResponse, WorkerItem, ReExtractResponse,
)
from db.session import get_session
from db.models import Document
from sqlalchemy import func

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
            uuids_to_run: list[str] = []

            if body.uuids:
                if not body.uuids:
                    logger.warning("[API] 抽取触发: UUID 列表为空")
                    return ExtractionTriggerResponse(
                        triggered=0,
                        message="UUID 列表为空",
                    )

                # 轻量校验 UUID 是否存在（不拉 raw_content）
                existing = (
                    session.query(Document.uuid)
                    .filter(
                        Document.uuid.in_(body.uuids),
                        Document.is_deleted == False,
                    )
                    .all()
                )
                valid_uuids = [r.uuid for r in existing]
                not_found = set(body.uuids) - set(valid_uuids)
                if not_found:
                    logger.warning(f"[API] 抽取触发: 以下 UUID 不存在: {not_found}")

                if valid_uuids:
                    # 批量更新状态，不逐行加载
                    session.query(Document).filter(
                        Document.uuid.in_(valid_uuids),
                        Document.is_deleted == False,
                    ).update(
                        {"extraction_status": "processing", "extraction_error": ""},
                        synchronize_session=False,
                    )
                    uuids_to_run = valid_uuids
            else:
                # 只查 UUID，raw_content 非空过滤直接在 DB 完成
                pending = (
                    session.query(Document.uuid)
                    .filter(
                        Document.extraction_status == "pending",
                        Document.is_deleted == False,
                        Document.raw_content != "",
                        Document.raw_content.isnot(None),
                    )
                    .limit(body.limit)
                    .all()
                )
                if pending:
                    pending_uuids = [r.uuid for r in pending]
                    session.query(Document).filter(
                        Document.uuid.in_(pending_uuids),
                    ).update(
                        {"extraction_status": "processing", "extraction_error": ""},
                        synchronize_session=False,
                    )
                    uuids_to_run = pending_uuids

            count = len(uuids_to_run)
            logger.info(f"[Extraction] 标记 processing: {count} 篇")

        except Exception as e:
            logger.error(f"[Extraction] 查询文档失败: {e}")
            raise HTTPException(status_code=500, detail=f"查询文档失败: {str(e)}")

    # 异步执行抽取（worker 各自加载 raw_content，压力分散）
    if count > 0:
        try:
            from flows.extraction_flow import run_extraction_for_uuids
            run_extraction_for_uuids(uuids_to_run)
            logger.info(f"[Extraction] 已提交 {count} 篇到抽取队列")
        except Exception as e:
            logger.error(f"[Extraction] 提交抽取任务失败: {e}")

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
                return session.query(func.count(Document.id)).filter(
                    Document.extraction_status == status,
                    Document.is_deleted == False,
                ).scalar()

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


@router.get("/db-pool")
def get_db_pool_status():
    """
    查询当前 DB 连接池状态（pg_stat_activity）。
    使用原生 psycopg2 连接，绕过 SQLAlchemy 连接池，池满时也能访问。
    """
    import psycopg2
    from config.settings import APP

    try:
        conn = psycopg2.connect(APP.DB_URL, connect_timeout=5)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE state = 'active') AS active,
                   count(*) FILTER (WHERE state = 'idle') AS idle,
                   count(*) FILTER (WHERE wait_event_type = 'Lock') AS waiting_lock
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
        """)
        row = cur.fetchone()
        summary = {"total": row[0], "active": row[1], "idle": row[2], "waiting_lock": row[3]}

        cur.execute("""
            SELECT pid, state, wait_event_type,
                   age(now(), query_start) AS duration,
                   left(query, 120) AS query_preview
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
            ORDER BY query_start
        """)
        details = [
            {
                "pid": r[0],
                "state": r[1],
                "wait_event": r[2],
                "duration": str(r[3]) if r[3] else None,
                "query": r[4],
            }
            for r in cur.fetchall()
        ]

        cur.close()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")

    return {"summary": summary, "connections": details}


@router.post("/re-extract/{uuid}", response_model=ReExtractResponse)
def re_extract_document(uuid: str):
    """
    触发单文档重抽取。
    - 检查 extraction_status 不能是 "processing"
    - 重置为 "processing" 并提交到 WorkerManager
    - 旧 extracted_entities / entity_relations 保留不删
    """
    if not uuid or not uuid.strip():
        raise HTTPException(status_code=400, detail="uuid 不能为空")

    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        doc = session.query(Document).filter(
            Document.uuid == uuid,
            Document.is_deleted == False,
        ).first()

        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")

        if doc.extraction_status == "processing":
            raise HTTPException(status_code=409, detail="文档正在抽取中，请等待完成后再重试")

        if not doc.raw_content or not doc.raw_content.strip():
            raise HTTPException(status_code=400, detail="文档 raw_content 为空，无法抽取")

        doc.extraction_status = "processing"
        doc.extraction_error = ""
        doc.extraction_model = ""

    # 提交到 WorkerManager
    try:
        from flows.extraction_flow import run_extraction_for_uuids
        result = run_extraction_for_uuids([uuid])
        if result.get("failed", 0) > 0:
            raise HTTPException(status_code=500, detail=result.get("error", "提交失败"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 重抽取提交失败: {uuid} | {e}")
        raise HTTPException(status_code=500, detail=f"提交失败: {str(e)}")

    logger.info(f"[API] 重抽取已提交: {uuid}")
    return ReExtractResponse(success=True, uuid=uuid, message="已重新提交抽取")


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
        doc = (
            session.query(
                Document.uuid, Document.extraction_status, Document.extraction_model,
                Document.extracted_at, Document.extraction_error,
            )
            .filter(
                Document.uuid == uuid,
                Document.is_deleted == False
            ).first()
        )
        
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
