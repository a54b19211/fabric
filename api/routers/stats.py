"""统计仪表盘接口"""
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query

from api.schemas.response import (
    DashboardResponse, SourceDistribution,
    EntityListResponse, EntityItem,
    TopEntitiesResponse, TopEntityItem,
)
from db.repository import (
    is_db_connected, get_dashboard_stats, get_top_entities,
)

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard():
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = get_dashboard_stats(today)
    return DashboardResponse(
        total_documents=d["total_documents"],
        today_new=d["today_new"],
        source_distribution=[SourceDistribution(**s) for s in d["source_distribution"]],
        review_stats=EntityListResponse(total=0, items=[]),
        entity_count=d["entity_count"],
        relation_count=d["relation_count"],
        avg_entity_confidence=d["avg_entity_confidence"],
        review_pass_rate=d["review_pass_rate"],
        neo4j_pending_entities=d["neo4j_pending_entities"],
        neo4j_pending_relations=d["neo4j_pending_relations"],
        neo4j_failed_total=d["neo4j_failed_total"],
        weknora_pending=d["weknora_pending"],
        weknora_synced=d["weknora_synced"],
        weknora_failed=d["weknora_failed"],
        extraction_pending=d["extraction_pending"],
        extraction_done=d["extraction_done"],
    )


@router.get("/top-entities", response_model=TopEntitiesResponse)
def get_top_entities_api(
    entity_type: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    rows = get_top_entities(entity_type=entity_type, limit=limit)
    items = [TopEntityItem(**r) for r in rows]
    return TopEntitiesResponse(items=items)
