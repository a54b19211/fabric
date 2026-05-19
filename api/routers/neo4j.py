"""Neo4j 推送接口"""
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.schemas.request import Neo4jSyncRequest, Neo4jRetryRequest
from api.schemas.response import (
    Neo4jSyncResponse, Neo4jRetryResponse, Neo4jStatsResponse,
    Neo4jStatusCount, Neo4jFailedResponse,
    FailedEntityItem, FailedRelationItem,
)
from db.repository import (
    get_approved_for_neo4j, get_approved_relations_for_neo4j,
    mark_entity_pushed, mark_entity_failed,
    mark_relation_pushed, mark_relation_failed,
)
from db.session import get_session
from db.models import ExtractedEntity, EntityRelation

router = APIRouter(prefix="/api/neo4j", tags=["neo4j"])


@router.post("/sync/entities", response_model=Neo4jSyncResponse)
def sync_entities(body: Neo4jSyncRequest):
    """批量推送审核通过的实体到 Neo4j。"""
    entities = get_approved_for_neo4j(limit=body.limit)
    pushed, failed = 0, 0

    for ent in entities:
        try:
            # TODO: 接入实际 Neo4j 推送逻辑（成员1交付后对接）
            # neo4j_node_id = neo4j_client.push_entity(ent)
            # mark_entity_pushed(ent["id"], neo4j_node_id)
            logger.info(f"[Neo4j] 推送实体（占位）: id={ent['id']}")
            pushed += 1
        except Exception as e:
            mark_entity_failed(ent["id"], str(e))
            failed += 1

    return Neo4jSyncResponse(pushed=pushed, failed=failed, message=f"实体推送完成")


@router.post("/sync/relations", response_model=Neo4jSyncResponse)
def sync_relations(body: Neo4jSyncRequest):
    """批量推送审核通过的关系到 Neo4j。"""
    relations = get_approved_relations_for_neo4j(limit=body.limit)
    pushed, failed = 0, 0

    for rel in relations:
        try:
            # TODO: 接入实际 Neo4j 推送逻辑
            logger.info(f"[Neo4j] 推送关系（占位）: id={rel['id']}")
            pushed += 1
        except Exception as e:
            mark_relation_failed(rel["id"], str(e))
            failed += 1

    return Neo4jSyncResponse(pushed=pushed, failed=failed, message=f"关系推送完成")


@router.post("/retry", response_model=Neo4jRetryResponse)
def retry_push(body: Neo4jRetryRequest):
    """重试单条推送失败的实体或关系。"""
    try:
        if body.object_type == "entity":
            # TODO: 接入实际推送逻辑
            logger.info(f"[Neo4j] 重试实体: id={body.object_id}")
        elif body.object_type == "relation":
            logger.info(f"[Neo4j] 重试关系: id={body.object_id}")
        else:
            raise HTTPException(status_code=400, detail="object_type 必须是 entity 或 relation")
        return Neo4jRetryResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        return Neo4jRetryResponse(success=False, error=str(e))


@router.get("/stats", response_model=Neo4jStatsResponse)
def get_neo4j_stats():
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        def _ent_count(status: str) -> int:
            return session.query(ExtractedEntity).filter(
                ExtractedEntity.neo4j_status == status,
                ExtractedEntity.is_deleted == False,
            ).count()

        def _rel_count(status: str) -> int:
            return session.query(EntityRelation).filter(
                EntityRelation.neo4j_status == status,
                EntityRelation.is_deleted == False,
            ).count()

        return Neo4jStatsResponse(
            entities=Neo4jStatusCount(
                pending=_ent_count("pending"),
                pushed=_ent_count("pushed"),
                failed=_ent_count("failed"),
            ),
            relations=Neo4jStatusCount(
                pending=_rel_count("pending"),
                pushed=_rel_count("pushed"),
                failed=_rel_count("failed"),
            ),
        )


@router.get("/failed", response_model=Neo4jFailedResponse)
def get_failed(
    object_type: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
):
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        entities, relations = [], []

        if object_type in ("", "entity"):
            rows = session.query(ExtractedEntity).filter(
                ExtractedEntity.neo4j_status == "failed",
                ExtractedEntity.is_deleted == False,
            ).limit(limit).all()
            entities = [
                FailedEntityItem(
                    id=r.id,
                    entity_name=r.entity_name,
                    entity_type=r.entity_type,
                    neo4j_error=r.neo4j_error,
                    neo4j_pushed_at=r.neo4j_pushed_at.isoformat() if r.neo4j_pushed_at else None,
                )
                for r in rows
            ]

        if object_type in ("", "relation"):
            rows = session.query(EntityRelation).filter(
                EntityRelation.neo4j_status == "failed",
                EntityRelation.is_deleted == False,
            ).limit(limit).all()
            relations = [
                FailedRelationItem(
                    id=r.id,
                    relation_type=r.relation_type,
                    from_entity_id=r.from_entity_id,
                    to_entity_id=r.to_entity_id,
                    neo4j_error=r.neo4j_error,
                    neo4j_pushed_at=r.neo4j_pushed_at.isoformat() if r.neo4j_pushed_at else None,
                )
                for r in rows
            ]

        return Neo4jFailedResponse(entities=entities, relations=relations)
