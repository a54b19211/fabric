"""WeKnora 同步接口"""
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.schemas.request import WeknoraBatchRequest
from api.schemas.response import (
    WeknoraPendingResponse, WeknoraPendingItem,
    WeknoraSyncResponse, WeknoraBatchResponse, WeknoraStatsResponse,
)
from db.repository import update_weknora_sync_status, get_pending_weknora_sync
from db.session import get_session
from db.models import Document

router = APIRouter(prefix="/api/weknora", tags=["weknora"])


@router.get("/pending", response_model=WeknoraPendingResponse)
def get_pending(limit: int = Query(100, ge=1, le=500)):
    rows = get_pending_weknora_sync(limit=limit)
    items = [
        WeknoraPendingItem(
            uuid=r["uuid"],
            title=r["title"],
            source_site=r["source_site"],
            publish_date=r["publish_date"],
        )
        for r in rows
    ]
    return WeknoraPendingResponse(total=len(items), items=items)


@router.post("/sync/{uuid}", response_model=WeknoraSyncResponse)
def sync_single(uuid: str):
    """手动触发单篇文档同步到 WeKnora。"""
    try:
        # TODO: 接入实际 sync_to_weknora 逻辑
        logger.info(f"[WeKnora] 同步文档（占位）: {uuid}")
        update_weknora_sync_status(uuid, "synced")
        return WeknoraSyncResponse(success=True, message="同步成功")
    except Exception as e:
        update_weknora_sync_status(uuid, "failed")
        return WeknoraSyncResponse(success=False, message=str(e))


@router.post("/sync-batch", response_model=WeknoraBatchResponse)
def sync_batch(body: WeknoraBatchRequest):
    """批量触发同步，异步执行，立即返回。"""
    rows = get_pending_weknora_sync(limit=body.limit)
    # TODO: 接入实际批量同步逻辑
    logger.info(f"[WeKnora] 批量同步触发（占位）: {len(rows)} 篇")
    return WeknoraBatchResponse(triggered=len(rows))


@router.get("/stats", response_model=WeknoraStatsResponse)
def get_stats():
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        def _count(status: str) -> int:
            return session.query(Document).filter(
                Document.weknora_sync_status == status,
                Document.is_deleted == False,
            ).count()

        return WeknoraStatsResponse(
            pending=_count("pending"),
            synced=_count("synced"),
            failed=_count("failed"),
        )
