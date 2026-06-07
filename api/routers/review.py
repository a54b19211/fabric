# -*- coding: utf-8 -*-
"""审核操作接口"""
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.schemas.request import (
    ReviewActionRequest,
    BatchApproveEntitiesRequest,
    BatchRejectEntitiesRequest,
    BatchApproveRelationsRequest,
    BatchRejectRelationsRequest,
    ResolveMergeRequest,
    PatchEntityRequest,
    PatchRelationRequest,
)
from api.schemas.response import (
    EntityListResponse, EntityItem,
    RelationListResponse, RelationItem,
    ReviewActionResponse,
    DisambigInfo, DisambigCandidate,
    MergeQueueItem, MergeQueueListResponse,
    DictCandidateItem, DictCandidateListResponse,
    GraphResponse, GraphNode, GraphEdge,
    ReviewStatsResponse, StatusCounts,
    ReviewLogListResponse, ReviewLogItem,
)
from db.repository import (
    is_db_connected,
    approve_entity_with_alias_writeback,
    approve_entity, reject_entity,
    approve_relation, reject_relation,
    batch_approve_entities, batch_reject_entities,
    batch_approve_relations, batch_reject_relations,
    log_review_action,
    get_pending_merges, resolve_merge,
    find_candidate_dicts_for_review,
    list_entities, get_entity_by_id, get_entity_dict_by_id, get_aliases_for_dict,
    get_review_graph, get_review_stats,
    list_relations, get_relation_by_id,
    get_review_logs,
    patch_entity_data, patch_relation_props,
)

router = APIRouter(prefix="/api/review", tags=["review"])


def _extract_disambig(entity_data: dict | None) -> DisambigInfo | None:
    if not entity_data:
        return None
    raw = entity_data.get("_disambig")
    if not raw:
        return None
    try:
        return DisambigInfo(
            status=raw.get("status", ""),
            candidates=[DisambigCandidate(**c) for c in raw.get("candidates", [])],
            match_step=raw.get("match_step", ""),
        )
    except Exception:
        return None


def _enrich_entity(entity) -> EntityItem:
    canonical_data = None
    canonical_source = None
    aliases = None
    if entity.dict_id:
        ed = get_entity_dict_by_id(entity.dict_id)
        if ed:
            canonical_data = ed.canonical_data
            canonical_source = ed.canonical_source
            aliases = get_aliases_for_dict(ed.id)
    return EntityItem(
        id=entity.id,
        source_doc_uuid=entity.source_doc_uuid,
        entity_type=entity.entity_type,
        entity_name=entity.entity_name,
        entity_data=entity.entity_data,
        confidence_score=entity.confidence_score,
        confidence_reason=entity.confidence_reason,
        source_text=entity.source_text,
        review_status=entity.review_status,
        dict_id=entity.dict_id,
        disambig=_extract_disambig(entity.entity_data),
        canonical_data=canonical_data,
        canonical_source=canonical_source,
        aliases=aliases,
    )


# ── 实体 ───────────────────────────────────────────────────────────────

@router.get("/entities", response_model=EntityListResponse)
def list_entities_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    entity_type: str = Query(""),
    source_site: str = Query(""),
    source_doc_uuid: str = Query(""),
    review_status: str = Query("pending"),
):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    result = list_entities(
        page=page, page_size=page_size, entity_type=entity_type,
        source_site=source_site, source_doc_uuid=source_doc_uuid,
        review_status=review_status,
    )
    items = [_enrich_entity(r) for r in result["items"]]
    return EntityListResponse(total=result["total"], items=items)


@router.get("/entities/{entity_id}", response_model=EntityItem)
def get_entity(entity_id: int):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    row = get_entity_by_id(entity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="实体不存在")
    return _enrich_entity(row)


@router.post("/entities/batch/approve", response_model=ReviewActionResponse)
def batch_approve_entities_api(body: BatchApproveEntitiesRequest):
    try:
        result = batch_approve_entities(
            source_doc_uuid=body.source_doc_uuid or "",
            entity_type=body.entity_type or "",
            source_site=body.source_site or "",
            reviewer=body.reviewer,
            note=body.note or "",
            min_confidence=body.min_confidence,
        )
        if not result.get("success", False):
            raise HTTPException(status_code=500, detail=result.get("message", "审核失败"))
        return ReviewActionResponse(success=True, message=result.get("message", ""))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量审核实体失败: {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/entities/batch/reject", response_model=ReviewActionResponse)
def batch_reject_entities_api(body: BatchRejectEntitiesRequest):
    try:
        result = batch_reject_entities(
            source_doc_uuid=body.source_doc_uuid or "",
            entity_type=body.entity_type or "",
            source_site=body.source_site or "",
            reviewer=body.reviewer,
            note=body.note or "",
        )
        if not result.get("success", False):
            raise HTTPException(status_code=500, detail=result.get("message", "审核失败"))
        return ReviewActionResponse(success=True, message=result.get("message", ""))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量审核实体失败: {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/entities/{entity_id}/approve", response_model=ReviewActionResponse)
def approve_entity_api(entity_id: int, body: ReviewActionRequest):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    if get_entity_by_id(entity_id) is None:
        raise HTTPException(status_code=404, detail="实体不存在")
    try:
        approve_entity_with_alias_writeback(
            entity_id, reviewer=body.reviewer, note=body.note or "",
            dict_id=body.dict_id, force_new=body.force_new,
        )
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 审核实体失败: id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/entities/{entity_id}/reject", response_model=ReviewActionResponse)
def reject_entity_api(entity_id: int, body: ReviewActionRequest):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    if get_entity_by_id(entity_id) is None:
        raise HTTPException(status_code=404, detail="实体不存在")
    try:
        reject_entity(entity_id, reviewer=body.reviewer, note=body.note or "")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 审核实体失败: id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.get("/entities/{entity_id}/candidates", response_model=DictCandidateListResponse)
def get_entity_candidates(entity_id: int, top_k: int = Query(10, ge=1, le=50)):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    row = get_entity_by_id(entity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="实体不存在")

    entity_name = row.entity_name or ""
    entity_type = row.entity_type or ""
    entity_data = row.entity_data or {}

    if not entity_name or not entity_type:
        return DictCandidateListResponse(total=0, items=[])

    try:
        candidates = find_candidate_dicts_for_review(
            entity_name=entity_name, entity_type=entity_type,
            entity_data=entity_data, top_k=top_k,
        )
        items = [DictCandidateItem(**c) for c in candidates]
        return DictCandidateListResponse(total=len(items), items=items)
    except Exception as e:
        logger.error(f"[API] 实体候选查询失败: entity_id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=f"候选查询失败: {str(e)}")


# ── 关系 ───────────────────────────────────────────────────────────────

@router.get("/relations", response_model=RelationListResponse)
def list_relations_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    source_site: str = Query(""),
    source_doc_uuid: str = Query(""),
    review_status: str = Query("pending"),
):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    try:
        result = list_relations(
            page=page, page_size=page_size, source_site=source_site,
            source_doc_uuid=source_doc_uuid, review_status=review_status,
        )
        items = [RelationItem(**it) for it in result["items"]]
        return RelationListResponse(total=result["total"], items=items)
    except Exception as e:
        logger.error(f"[API] 关系列表查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/relations/batch/approve", response_model=ReviewActionResponse)
def batch_approve_relations_api(body: BatchApproveRelationsRequest):
    try:
        result = batch_approve_relations(
            source_doc_uuid=body.source_doc_uuid or "",
            source_site=body.source_site or "",
            reviewer=body.reviewer, note=body.note or "",
            min_confidence=body.min_confidence,
        )
        if not result.get("success", False):
            raise HTTPException(status_code=500, detail=result.get("message", "审核失败"))
        return ReviewActionResponse(success=True, message=result.get("message", ""))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量审核关系失败: {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/relations/batch/reject", response_model=ReviewActionResponse)
def batch_reject_relations_api(body: BatchRejectRelationsRequest):
    try:
        result = batch_reject_relations(
            source_doc_uuid=body.source_doc_uuid or "",
            source_site=body.source_site or "",
            reviewer=body.reviewer, note=body.note or "",
        )
        if not result.get("success", False):
            raise HTTPException(status_code=500, detail=result.get("message", "审核失败"))
        return ReviewActionResponse(success=True, message=result.get("message", ""))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量审核关系失败: {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/relations/{relation_id}/approve", response_model=ReviewActionResponse)
def approve_relation_api(relation_id: int, body: ReviewActionRequest):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    if get_relation_by_id(relation_id) is None:
        raise HTTPException(status_code=404, detail="关系不存在")
    try:
        approve_relation(relation_id, reviewer=body.reviewer, note=body.note or "")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 审核关系失败: id={relation_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/relations/{relation_id}/reject", response_model=ReviewActionResponse)
def reject_relation_api(relation_id: int, body: ReviewActionRequest):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    if get_relation_by_id(relation_id) is None:
        raise HTTPException(status_code=404, detail="关系不存在")
    try:
        reject_relation(relation_id, reviewer=body.reviewer, note=body.note or "")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 审核关系失败: id={relation_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


# ── 合并队列 ─────────────────────────────────────────────────────────

@router.get("/merge-queue", response_model=MergeQueueListResponse)
def list_merge_queue(limit: int = Query(50, ge=1, le=200)):
    try:
        rows = get_pending_merges(limit)
        items = [MergeQueueItem(**r) for r in rows]
        return MergeQueueListResponse(total=len(items), items=items)
    except Exception as e:
        logger.error(f"[API] 合并队列查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/merge-queue/{merge_id}/resolve", response_model=ReviewActionResponse)
def resolve_merge_api(merge_id: int, body: ResolveMergeRequest):
    try:
        resolve_merge(merge_id, body.resolution, body.reviewer, body.resolved_value)
        return ReviewActionResponse(success=True)
    except Exception as e:
        logger.error(f"[API] 合并冲突解决失败: merge_id={merge_id} | {e}")
        raise HTTPException(status_code=500, detail=f"解决失败: {str(e)}")


# ── 图谱 ──────────────────────────────────────────────────────────────

@router.get("/graph", response_model=GraphResponse)
def get_review_graph_api(
    entity_types: str = Query(""),
    source_site: str = Query(""),
    review_status: str = Query("pending,approved"),
    source_doc_uuid: str = Query(""),
    include_relations: bool = Query(True),
    limit: int = Query(500, ge=1, le=2000),
):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    statuses = [s.strip() for s in review_status.split(",") if s.strip()] or ["pending", "approved"]
    type_list = [t.strip() for t in entity_types.split(",") if t.strip()] or None
    result = get_review_graph(
        entity_types=type_list, source_site=source_site,
        source_doc_uuid=source_doc_uuid, review_statuses=statuses,
        include_relations=include_relations, limit=limit,
    )
    nodes = [GraphNode(**n) for n in result["nodes"]]
    edges = [GraphEdge(**e) for e in result["edges"]]
    return GraphResponse(nodes=nodes, edges=edges)


@router.get("/stats", response_model=ReviewStatsResponse)
def get_review_stats_api():
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    s = get_review_stats()
    return ReviewStatsResponse(
        entities=StatusCounts(**s["entities"]),
        relations=StatusCounts(**s["relations"]),
        by_type={k: StatusCounts(**v) for k, v in s["by_type"].items()},
        by_site={k: StatusCounts(**v) for k, v in s["by_site"].items()},
    )


@router.patch("/entities/{entity_id}", response_model=ReviewActionResponse)
def patch_entity(entity_id: int, body: PatchEntityRequest):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    if not patch_entity_data(entity_id, body.entity_data):
        raise HTTPException(status_code=404, detail="实体不存在")
    return ReviewActionResponse(success=True, message="实体元数据已更新")


@router.patch("/relations/{relation_id}", response_model=ReviewActionResponse)
def patch_relation(relation_id: int, body: PatchRelationRequest):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    if not patch_relation_props(relation_id, body.relation_props):
        raise HTTPException(status_code=404, detail="关系不存在")
    return ReviewActionResponse(success=True, message="关系元数据已更新")


# ── 审核历史 ──────────────────────────────────────────────────────────

@router.get("/logs", response_model=ReviewLogListResponse)
def get_review_logs_api(
    object_type: str = Query(""),
    object_id: int = Query(None),
    action: str = Query(""),
    reviewer: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
):
    if not is_db_connected():
        raise HTTPException(status_code=503, detail="数据库未连接")
    result = get_review_logs(
        object_type=object_type, object_id=object_id,
        action=action, reviewer=reviewer, limit=limit,
    )
    items = [ReviewLogItem(**it) for it in result["items"]]
    return ReviewLogListResponse(total=result["total"], items=items)
