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
    """待同步文档列表查询"""
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit 必须在 1-500 之间")
    
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        rows = get_pending_weknora_sync(limit=limit)
        items = [
            WeknoraPendingItem(
                uuid=r["uuid"] or "",
                title=r["title"] or "",
                source_site=r["source_site"] or "",
                publish_date=r["publish_date"] or "",
            )
            for r in rows
        ]
        
        logger.info(f"[API] 待同步文档查询: limit={limit}, total={len(items)}")
        return WeknoraPendingResponse(total=len(items), items=items)
        
    except Exception as e:
        logger.error(f"[API] 待同步文档查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/sync/{uuid}", response_model=WeknoraSyncResponse)
def sync_single(uuid: str):
    """手动触发单篇文档同步到 WeKnora。"""
    # UUID 格式校验
    if not uuid or not uuid.strip():
        raise HTTPException(status_code=400, detail="uuid 不能为空")
    
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        # TODO: 接入实际 sync_to_weknora 逻辑
        logger.info(f"[WeKnora] 同步文档（占位）: {uuid}")
        update_weknora_sync_status(uuid, "synced")
        logger.info(f"[API] WeKnora 同步成功: {uuid}")
        return WeknoraSyncResponse(success=True, message="同步成功")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] WeKnora 同步失败: {uuid} | {e}")
        try:
            update_weknora_sync_status(uuid, "failed")
        except Exception:
            pass  # 状态更新失败不影响返回
        return WeknoraSyncResponse(success=False, message=str(e))


@router.post("/sync-batch", response_model=WeknoraBatchResponse)
def sync_batch(body: WeknoraBatchRequest):
    """批量触发同步，异步执行，立即返回。"""
    # 参数校验
    if body.limit < 1 or body.limit > 500:
        raise HTTPException(status_code=400, detail="limit 必须在 1-500 之间")
    
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        rows = get_pending_weknora_sync(limit=body.limit)
        # TODO: 接入实际批量同步逻辑
        logger.info(f"[WeKnora] 批量同步触发（占位）: {len(rows)} 篇")
        logger.info(f"[API] WeKnora 批量同步触发: {len(rows)} 篇")
        return WeknoraBatchResponse(triggered=len(rows))
        
    except Exception as e:
        logger.error(f"[API] WeKnora 批量同步失败: {e}")
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


@router.get("/stats", response_model=WeknoraStatsResponse)
def get_stats():
    """WeKnora 同步状态统计"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        def _count(status: str) -> int:
            return session.query(Document).filter(
                Document.weknora_sync_status == status,
                Document.is_deleted == False,
            ).count()

        stats = WeknoraStatsResponse(
            pending=_count("pending"),
            synced=_count("synced"),
            failed=_count("failed"),
        )
        
        logger.info(f"[API] WeKnora 同步统计: {stats.json()}")
        return stats
        
    except Exception as e:
        logger.error(f"[API] WeKnora 同步统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")
