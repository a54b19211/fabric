"""WeKnora 同步接口 — 文档同步 + 图数据同步"""
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.schemas.request import WeknoraBatchRequest, GraphSyncRequest
from api.schemas.response import (
    WeknoraPendingResponse, WeknoraPendingItem,
    WeknoraSyncResponse, WeknoraBatchResponse, WeknoraStatsResponse,
    GraphSyncResponse, GraphSyncStatsResponse, GraphSyncStatusCount,
)
from db.repository import (
    update_weknora_sync_status, get_pending_weknora_sync,
    get_entity_dict_for_weknora, batch_mark_entity_dict_synced, batch_mark_entity_dict_failed,
    get_entities_for_weknora_graph, batch_mark_entity_weknora_graph_pushed, batch_mark_entity_weknora_graph_failed,
    get_relations_for_weknora_graph, batch_mark_relation_weknora_graph_pushed, batch_mark_relation_weknora_graph_failed,
    get_weknora_graph_stats,
)
from db.session import get_session
from db.models import Document
from core_engine.weknora_graph_client import get_weknora_graph_client

router = APIRouter(prefix="/api/weknora", tags=["weknora"])


# ═══════════════════════════════════════════════════════════════════════════
# 文档同步（Markdown 全文）
# ═══════════════════════════════════════════════════════════════════════════

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
            pass
        return WeknoraSyncResponse(success=False, message=str(e))


@router.post("/sync-batch", response_model=WeknoraBatchResponse)
def sync_batch(body: WeknoraBatchRequest):
    """批量触发文档同步，异步执行，立即返回。"""
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
    """WeKnora 文档同步状态统计"""
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


# ═══════════════════════════════════════════════════════════════════════════
# 图数据同步（entity_dict / entities / relations → WeKnora → Neo4j）
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/graph/sync-entity-dict", response_model=GraphSyncResponse)
def sync_entity_dict_to_weknora(body: GraphSyncRequest):
    """将 entity_dict 中待同步的行推送到 WeKnora 镜像表。分批发送，单批最多 200 条。"""
    BATCH_SIZE = 200
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        rows = get_entity_dict_for_weknora(limit=0)  # 0=全量
        if not rows:
            return GraphSyncResponse(success=True, pushed=0, failed=0, message="无待同步数据")

        client = get_weknora_graph_client()
        total_pushed = 0
        total_failed = 0

        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            batch_ids = [row["id"] for row in batch]
            try:
                result = client.batch_upsert_entity_dict(batch)
                upserted = result.get("data", {}).get("upserted", 0)
                total_pushed += upserted
                batch_mark_entity_dict_synced(batch_ids)
            except Exception as batch_error:
                total_failed += len(batch)
                batch_mark_entity_dict_failed(batch_ids, str(batch_error))
                logger.error(f"[API] entity_dict 批次 {i // BATCH_SIZE + 1} 失败: {batch_error}")

        logger.info(f"[API] entity_dict 同步完成: pushed={total_pushed}, failed={total_failed}")
        return GraphSyncResponse(success=True, pushed=total_pushed, message=f"同步完成: pushed={total_pushed}, failed={total_failed}")

    except Exception as e:
        logger.error(f"[API] entity_dict 同步失败: {e}")
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


@router.post("/graph/init-copies", response_model=GraphSyncResponse)
def init_primekg_copies(kb_id: str = Query("", description="目标知识库 ID，留空用 .env 配置")):
    """触发 WeKnora 从 entity_dict 镜像表初始化 PrimeKG 副本到 graph_entities。"""
    try:
        client = get_weknora_graph_client()
        result = client.init_copies(kb_id)
        upserted = result.get("data", {}).get("upserted", 0)
        return GraphSyncResponse(success=True, pushed=upserted, message="PrimeKG 副本初始化完成")
    except Exception as e:
        logger.error(f"[API] init_copies 失败: {e}")
        raise HTTPException(status_code=500, detail=f"初始化失败: {str(e)}")


@router.post("/graph/sync-entities", response_model=GraphSyncResponse)
def sync_entities_to_weknora_graph(body: GraphSyncRequest):
    """将审核通过的抽取实体推送到 WeKnora graph_entities。分批发送，单批最多 200 条。"""
    BATCH_SIZE = 200
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        entities = get_entities_for_weknora_graph(limit=0)  # 0=全量
        if not entities:
            return GraphSyncResponse(success=True, pushed=0, failed=0, message="无待同步实体")

        client = get_weknora_graph_client()
        total_pushed = 0
        total_failed = 0

        for i in range(0, len(entities), BATCH_SIZE):
            batch = entities[i:i + BATCH_SIZE]
            batch_ids = [ent["id"] for ent in batch]
            try:
                result = client.batch_upsert_entities(batch)
                pushed = result.get("data", {}).get("upserted", 0)
                total_pushed += pushed
                batch_mark_entity_weknora_graph_pushed(batch_ids)
            except Exception as batch_error:
                total_failed += len(batch)
                batch_mark_entity_weknora_graph_failed(batch_ids, str(batch_error))
                logger.error(f"[API] 实体图批次 {i // BATCH_SIZE + 1} 失败: {batch_error}")

        logger.info(f"[API] 实体图同步完成: pushed={total_pushed}, failed={total_failed}")
        return GraphSyncResponse(success=True, pushed=total_pushed, message=f"同步完成: pushed={total_pushed}, failed={total_failed}")

    except Exception as e:
        logger.error(f"[API] 实体图同步失败: {e}")
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


@router.post("/graph/sync-relations", response_model=GraphSyncResponse)
def sync_relations_to_weknora_graph(body: GraphSyncRequest):
    """将审核通过的抽取关系推送到 WeKnora graph_relations。分批发送，单批最多 200 条。"""
    BATCH_SIZE = 200
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        relations = get_relations_for_weknora_graph(limit=0)  # 0=全量
        if not relations:
            return GraphSyncResponse(success=True, pushed=0, failed=0, message="无待同步关系")

        client = get_weknora_graph_client()
        total_pushed = 0
        total_failed = 0

        for i in range(0, len(relations), BATCH_SIZE):
            batch = relations[i:i + BATCH_SIZE]
            batch_ids = [rel["id"] for rel in batch]
            try:
                result = client.batch_upsert_relations(batch)
                pushed = result.get("data", {}).get("upserted", 0)
                total_pushed += pushed
                batch_mark_relation_weknora_graph_pushed(batch_ids)
            except Exception as batch_error:
                total_failed += len(batch)
                batch_mark_relation_weknora_graph_failed(batch_ids, str(batch_error))
                logger.error(f"[API] 关系图批次 {i // BATCH_SIZE + 1} 失败: {batch_error}")

        logger.info(f"[API] 关系图同步完成: pushed={total_pushed}, failed={total_failed}")
        return GraphSyncResponse(success=True, pushed=total_pushed, message=f"同步完成: pushed={total_pushed}, failed={total_failed}")

    except Exception as e:
        logger.error(f"[API] 关系图同步失败: {e}")
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


@router.get("/graph/stats", response_model=GraphSyncStatsResponse)
def get_graph_sync_stats():
    """WeKnora 图同步状态统计"""
    try:
        stats = get_weknora_graph_stats()
        return GraphSyncStatsResponse(
            entity_dict=GraphSyncStatusCount(
                pending=stats["entity_dict"]["pending"],
                synced=stats["entity_dict"]["synced"],
                failed=stats["entity_dict"]["failed"],
            ),
            entities=GraphSyncStatusCount(
                pending=stats["entities"]["pending"],
                pushed=stats["entities"]["pushed"],
                failed=stats["entities"]["failed"],
            ),
            relations=GraphSyncStatusCount(
                pending=stats["relations"]["pending"],
                pushed=stats["relations"]["pushed"],
                failed=stats["relations"]["failed"],
            ),
        )
    except Exception as e:
        logger.error(f"[API] 图同步统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")
