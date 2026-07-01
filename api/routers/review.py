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
    BatchResolveMergeRequest,
    MergeEntitiesRequest,
    RollbackMergeRequest,
)
from api.schemas.response import (
    EntityListResponse, EntityItem,
    RelationListResponse, RelationItem,
    ReviewActionResponse,
    DisambigInfo, DisambigCandidate,
    MergeQueueGroup, MergeQueueListResponse,
    DictCandidateItem, DictCandidateListResponse,
    GraphResponse, GraphNode, GraphEdge,
    ReviewStatsResponse, StatusCounts,
    SearchEntityItem,
    CompareEntitiesResponse,
    MergeEntitiesResponse,
    RollbackMergeResponse,
    MergeLogItem, MergeLogListResponse,
)
from db.repository import (
    approve_entity_with_alias_writeback,
    approve_entity, reject_entity,
    approve_relation, reject_relation,
    batch_approve_entities, batch_reject_entities,
    batch_approve_relations, batch_reject_relations,
    log_review_action,
    get_pending_merges, resolve_merge, batch_resolve_merges,
    find_candidate_dicts_for_review,
    search_entity_dicts, compare_entities,
    merge_entities, rollback_merge,
    get_merge_logs,
)
from db.session import get_session
from db.models import ExtractedEntity, EntityRelation, EntityDict
from collections import deque
from sqlalchemy import func
from sqlalchemy.orm import aliased

router = APIRouter(prefix="/api/review", tags=["review"])


def _extract_disambig(entity_data: dict | None) -> DisambigInfo | None:
    """从 entity_data JSONB 中提取 _disambig 消歧元数据。"""
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
    except Exception as e:
        logger.warning(f"[Review] _disambig 解析失败: {e}")
        return None


# ── 实体 ───────────────────────────────────────────────────────────────

@router.get("/entities", response_model=EntityListResponse)
def list_entities(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    entity_type: str = Query(""),
    source_site: str = Query(""),
    source_doc_uuid: str = Query(""),
    review_status: str = Query("pending"),
):
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        q = session.query(ExtractedEntity).filter(
            ExtractedEntity.review_status == review_status,
            ExtractedEntity.is_deleted == False,
        )
        if entity_type:
            q = q.filter(ExtractedEntity.entity_type == entity_type)
        if source_site:
            q = q.filter(ExtractedEntity.source_site == source_site)
        if source_doc_uuid:
            q = q.filter(ExtractedEntity.source_doc_uuid == source_doc_uuid)

        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()

        items = [
            EntityItem(
                id=r.id,
                source_doc_uuid=r.source_doc_uuid,
                entity_type=r.entity_type,
                entity_name=r.entity_name,
                entity_data=r.entity_data,
                confidence_score=r.confidence_score,
                confidence_reason=r.confidence_reason,
                source_text=r.source_text,
                review_status=r.review_status,
                dict_id=r.dict_id,
                disambig=_extract_disambig(r.entity_data),
            )
            for r in rows
        ]
        return EntityListResponse(total=total, items=items)


@router.get("/entities/{entity_id}", response_model=EntityItem)
def get_entity(entity_id: int):
    """实体详情查询"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            row = session.query(ExtractedEntity).filter(
                ExtractedEntity.id == entity_id,
                ExtractedEntity.is_deleted == False,
            ).first()
            
            if row is None:
                logger.warning(f"[API] 实体不存在: id={entity_id}")
                raise HTTPException(status_code=404, detail="实体不存在")

            return EntityItem(
                id=row.id,
                source_doc_uuid=row.source_doc_uuid or "",
                entity_type=row.entity_type or "",
                entity_name=row.entity_name or "",
                entity_data=row.entity_data or {},
                confidence_score=row.confidence_score or 0.0,
                confidence_reason=row.confidence_reason or "",
                source_text=row.source_text or "",
                review_status=row.review_status or "",
                dict_id=row.dict_id,
                disambig=_extract_disambig(row.entity_data),
            )
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[API] 实体详情查询失败: id={entity_id} | {e}")
            raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/entities/batch/approve", response_model=ReviewActionResponse)
def batch_approve_entities_api(body: BatchApproveEntitiesRequest):
    """批量审核通过实体"""
    try:
        result = batch_approve_entities(
            source_doc_uuid=body.source_doc_uuid or "",
            entity_type=body.entity_type or "",
            source_site=body.source_site or "",
            reviewer=body.reviewer,
            note=body.note or "",
            min_confidence=body.min_confidence
        )
        
        if not result.get("success", False):
            logger.warning(f"[API] 批量审核实体失败: {result.get('message', '未知错误')}")
            raise HTTPException(status_code=500, detail=result.get("message", "审核失败"))
        
        logger.info(f"[API] 批量审核实体通过: count={result.get('count', 0)}")
        return ReviewActionResponse(success=True, message=result.get("message", ""))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量审核实体失败: {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/entities/batch/reject", response_model=ReviewActionResponse)
def batch_reject_entities_api(body: BatchRejectEntitiesRequest):
    """批量审核拒绝实体"""
    try:
        result = batch_reject_entities(
            source_doc_uuid=body.source_doc_uuid or "",
            entity_type=body.entity_type or "",
            source_site=body.source_site or "",
            reviewer=body.reviewer,
            note=body.note or ""
        )
        
        if not result.get("success", False):
            logger.warning(f"[API] 批量审核实体失败: {result.get('message', '未知错误')}")
            raise HTTPException(status_code=500, detail=result.get("message", "审核失败"))
        
        logger.info(f"[API] 批量审核实体拒绝: count={result.get('count', 0)}")
        return ReviewActionResponse(success=True, message=result.get("message", ""))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量审核实体失败: {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/entities/{entity_id}/approve", response_model=ReviewActionResponse)
def approve_entity_api(entity_id: int, body: ReviewActionRequest):
    """审核通过单个实体（含 alias 回写 + embedding 生成）。"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")

    try:
        approve_entity_with_alias_writeback(
            entity_id, reviewer=body.reviewer, note=body.note or "",
            dict_id=body.dict_id,
            force_new=body.force_new,
        )
        logger.info(f"[API] 审核实体通过: id={entity_id}, reviewer={body.reviewer}, dict_id={body.dict_id}, force_new={body.force_new}")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 审核实体失败: id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/entities/{entity_id}/reject", response_model=ReviewActionResponse)
def reject_entity_api(entity_id: int, body: ReviewActionRequest):
    """审核拒绝单个实体"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")

    try:
        reject_entity(entity_id, reviewer=body.reviewer, note=body.note or "")
        logger.info(f"[API] 审核实体拒绝: id={entity_id}, reviewer={body.reviewer}")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 审核实体失败: id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.get("/entities/{entity_id}/candidates", response_model=DictCandidateListResponse)
def get_entity_candidates(
    entity_id: int,
    top_k: int = Query(10, ge=1, le=50),
):
    """
    审核页实时查询 entity_dict 中与该实体相似的候选项。
    不依赖预计算的 _disambig.candidates，而是动态查 entity_aliases + 向量相似度。
    返回的候选项包含 is_verified 标记，前端可区分"已审核"和"自动创建"。
    """
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")

        entity_name = row.entity_name or ""
        entity_type = row.entity_type or ""
        entity_data = row.entity_data or {}

    if not entity_name or not entity_type:
        return DictCandidateListResponse(total=0, items=[])

    try:
        candidates = find_candidate_dicts_for_review(
            entity_name=entity_name,
            entity_type=entity_type,
            entity_data=entity_data,
            top_k=top_k,
        )
        items = [DictCandidateItem(**c) for c in candidates]
        return DictCandidateListResponse(total=len(items), items=items)
    except Exception as e:
        logger.error(f"[API] 实体候选查询失败: entity_id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=f"候选查询失败: {str(e)}")


# ── 关系 ───────────────────────────────────────────────────────────────

@router.get("/relations", response_model=RelationListResponse)
def list_relations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    source_site: str = Query(""),
    source_doc_uuid: str = Query(""),
    review_status: str = Query("pending"),
):
    """关系列表查询（分页 + 筛选）"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            FromEnt = aliased(ExtractedEntity)
            ToEnt = aliased(ExtractedEntity)

            q = (
                session.query(
                    EntityRelation,
                    FromEnt.entity_name.label("from_name"),
                    ToEnt.entity_name.label("to_name"),
                )
                .outerjoin(FromEnt, EntityRelation.from_entity_id == FromEnt.id)
                .outerjoin(ToEnt, EntityRelation.to_entity_id == ToEnt.id)
                .filter(
                    EntityRelation.review_status == review_status,
                    EntityRelation.is_deleted == False,
                )
            )

            # 动态筛选条件
            if source_site:
                q = q.filter(EntityRelation.source_site == source_site)
            if source_doc_uuid:
                q = q.filter(EntityRelation.source_doc_uuid == source_doc_uuid)

            total = q.count()
            rows = q.offset((page - 1) * page_size).limit(page_size).all()

            items = [
                RelationItem(
                    id=r.EntityRelation.id,
                    source_doc_uuid=r.EntityRelation.source_doc_uuid or "",
                    relation_type=r.EntityRelation.relation_type or "",
                    from_entity_id=r.EntityRelation.from_entity_id,
                    from_entity_name=r.from_name or "",
                    to_entity_id=r.EntityRelation.to_entity_id,
                    to_entity_name=r.to_name or "",
                    relation_props=r.EntityRelation.relation_props or {},
                    confidence_score=r.EntityRelation.confidence_score or 0.0,
                    source_text=r.EntityRelation.source_text or "",
                    review_status=r.EntityRelation.review_status or "",
                )
                for r in rows
            ]
            
            logger.info(f"[API] 关系列表查询: page={page}, total={total}")
            return RelationListResponse(total=total, items=items)
            
        except Exception as e:
            logger.error(f"[API] 关系列表查询失败: {e}")
            raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/relations/batch/approve", response_model=ReviewActionResponse)
def batch_approve_relations_api(body: BatchApproveRelationsRequest):
    """批量审核通过关系"""
    try:
        result = batch_approve_relations(
            source_doc_uuid=body.source_doc_uuid or "",
            source_site=body.source_site or "",
            reviewer=body.reviewer,
            note=body.note or "",
            min_confidence=body.min_confidence
        )
        
        if not result.get("success", False):
            logger.warning(f"[API] 批量审核关系失败: {result.get('message', '未知错误')}")
            raise HTTPException(status_code=500, detail=result.get("message", "审核失败"))
        
        logger.info(f"[API] 批量审核关系通过: count={result.get('count', 0)}")
        return ReviewActionResponse(success=True, message=result.get("message", ""))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量审核关系失败: {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/relations/batch/reject", response_model=ReviewActionResponse)
def batch_reject_relations_api(body: BatchRejectRelationsRequest):
    """批量审核拒绝关系"""
    try:
        result = batch_reject_relations(
            source_doc_uuid=body.source_doc_uuid or "",
            source_site=body.source_site or "",
            reviewer=body.reviewer,
            note=body.note or ""
        )
        
        if not result.get("success", False):
            logger.warning(f"[API] 批量审核关系失败: {result.get('message', '未知错误')}")
            raise HTTPException(status_code=500, detail=result.get("message", "审核失败"))
        
        logger.info(f"[API] 批量审核关系拒绝: count={result.get('count', 0)}")
        return ReviewActionResponse(success=True, message=result.get("message", ""))
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量审核关系失败: {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/relations/{relation_id}/approve", response_model=ReviewActionResponse)
def approve_relation_api(relation_id: int, body: ReviewActionRequest):
    """审核通过单个关系"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        row = session.query(EntityRelation).filter(
            EntityRelation.id == relation_id,
            EntityRelation.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="关系不存在")

    try:
        approve_relation(relation_id, reviewer=body.reviewer, note=body.note or "")
        logger.info(f"[API] 审核关系通过: id={relation_id}, reviewer={body.reviewer}")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 审核关系失败: id={relation_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/relations/{relation_id}/reject", response_model=ReviewActionResponse)
def reject_relation_api(relation_id: int, body: ReviewActionRequest):
    """审核拒绝单个关系"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        row = session.query(EntityRelation).filter(
            EntityRelation.id == relation_id,
            EntityRelation.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="关系不存在")

    try:
        reject_relation(relation_id, reviewer=body.reviewer, note=body.note or "")
        logger.info(f"[API] 审核关系拒绝: id={relation_id}, reviewer={body.reviewer}")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 审核关系失败: id={relation_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


# ── 合并队列 ───────────────────────────────────────────────────────────────

@router.get("/merge-queue", response_model=MergeQueueListResponse)
def list_merge_queue(limit: int = Query(50, ge=1, le=200)):
    """查询待处理的合并冲突列表（按 dict_id 分组，含实体上下文）。"""
    try:
        result = get_pending_merges(limit)
        return MergeQueueListResponse(**result)
    except Exception as e:
        logger.error(f"[API] 合并队列查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/merge-queue/{merge_id}/resolve", response_model=ReviewActionResponse)
def resolve_merge_api(merge_id: int, body: ResolveMergeRequest):
    """解决单个合并冲突。resolution: accept_new | keep_existing | custom"""
    try:
        resolve_merge(merge_id, body.resolution, body.reviewer, body.resolved_value)
        logger.info(f"[API] 合并冲突解决: merge_id={merge_id} resolution={body.resolution}")
        return ReviewActionResponse(success=True)
    except Exception as e:
        logger.error(f"[API] 合并冲突解决失败: merge_id={merge_id} | {e}")
        raise HTTPException(status_code=500, detail=f"解决失败: {str(e)}")


@router.post("/merge-queue/resolve-batch", response_model=ReviewActionResponse)
def batch_resolve_merge_api(body: BatchResolveMergeRequest):
    """批量解决合并冲突：一次请求解决同 dict_id 下所有冲突字段。"""
    try:
        result = batch_resolve_merges(body.decisions, body.reviewer)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "批量解决失败"))
        logger.info(f"[API] 批量合并冲突解决: {result['resolved_count']} 条")
        return ReviewActionResponse(success=True, message=f"已解决 {result['resolved_count']} 条冲突")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 批量合并冲突解决失败: {e}")
        raise HTTPException(status_code=500, detail=f"批量解决失败: {str(e)}")


# ── 图谱 ─────────────────────────────────────────────────────────────────

@router.get("/graph", response_model=GraphResponse)
def get_review_graph(
    entity_types: str = Query("", description="逗号分隔的实体类型"),
    source_site: str = Query("", description="来源站点筛选"),
    review_status: str = Query("pending,approved", description="逗号分隔的审核状态"),
    source_doc_uuid: str = Query("", description="单文档 UUID"),
    include_relations: bool = Query(True, description="是否包含关系边"),
    limit: int = Query(500, ge=1, le=2000, description="最大节点数"),
    traversal: str = Query("bfs", description="遍历模式: bfs（默认，从种子逐跳展开）| degree（按度数降序）"),
    max_depth: int = Query(3, ge=1, le=10, description="BFS 最大跳数"),
):
    """图谱数据接口：返回待审核/已审核的实体节点和关系边。"""
    statuses = [s.strip() for s in review_status.split(",") if s.strip()]
    if not statuses:
        statuses = ["pending", "approved"]

    type_list = [t.strip() for t in entity_types.split(",") if t.strip()]

    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        # ── 公共：关系度数子查询 ──────────────────────────────────────────
        def _degree_subquery():
            from_deg = (
                session.query(
                    EntityRelation.from_entity_id.label('entity_id'),
                    func.count('*').label('degree'),
                )
                .filter(
                    EntityRelation.review_status.in_(statuses),
                    EntityRelation.is_deleted == False,
                )
                .group_by(EntityRelation.from_entity_id)
            )
            to_deg = (
                session.query(
                    EntityRelation.to_entity_id.label('entity_id'),
                    func.count('*').label('degree'),
                )
                .filter(
                    EntityRelation.review_status.in_(statuses),
                    EntityRelation.is_deleted == False,
                )
                .group_by(EntityRelation.to_entity_id)
            )
            union = from_deg.union_all(to_deg).subquery()
            return (
                session.query(
                    union.c.entity_id,
                    func.sum(union.c.degree).label('degree'),
                )
                .group_by(union.c.entity_id)
                .subquery()
            )

        degree_agg = _degree_subquery()

        # ── 公共：实体基础过滤 ────────────────────────────────────────────
        def _entity_filter(q):
            q = q.filter(
                ExtractedEntity.review_status.in_(statuses),
                ExtractedEntity.is_deleted == False,
            )
            if type_list:
                q = q.filter(ExtractedEntity.entity_type.in_(type_list))
            if source_site:
                q = q.filter(ExtractedEntity.source_site == source_site)
            if source_doc_uuid:
                q = q.filter(ExtractedEntity.source_doc_uuid == source_doc_uuid)
            return q

        # ── 公共：按实体 ID 拉取完整数据 ──────────────────────────────────
        _NODE_COLS = (
            ExtractedEntity.id,
            ExtractedEntity.entity_type,
            ExtractedEntity.entity_name,
            ExtractedEntity.entity_data,
            ExtractedEntity.dict_id,
            ExtractedEntity.review_status,
            ExtractedEntity.source_doc_uuid,
            ExtractedEntity.source_site,
            ExtractedEntity.confidence_score,
            EntityDict.is_verified,
        )

        def _fetch_nodes(entity_ids: list[int]) -> list[GraphNode]:
            rows = (
                session.query(*_NODE_COLS)
                .outerjoin(EntityDict, ExtractedEntity.dict_id == EntityDict.id)
                .filter(ExtractedEntity.id.in_(entity_ids))
                .all()
            )
            row_map = {r.id: r for r in rows}
            return [
                GraphNode(
                    id=row_map[eid].id,
                    entity_type=row_map[eid].entity_type or "",
                    entity_name=row_map[eid].entity_name or "",
                    entity_data=row_map[eid].entity_data or {},
                    dict_id=row_map[eid].dict_id,
                    review_status=row_map[eid].review_status or "",
                    source_doc_uuid=row_map[eid].source_doc_uuid or "",
                    source_site=row_map[eid].source_site or "",
                    confidence_score=row_map[eid].confidence_score or 0.0,
                    is_verified=row_map[eid].is_verified if row_map[eid].is_verified is not None else False,
                )
                for eid in entity_ids
            ]

        # ── 公共：边查询 ──────────────────────────────────────────────────
        def _fetch_edges(node_ids: list[int]) -> list[GraphEdge]:
            if not node_ids:
                return []
            rq = session.query(
                EntityRelation.id,
                EntityRelation.relation_type,
                EntityRelation.from_entity_id,
                EntityRelation.to_entity_id,
                EntityRelation.review_status,
                EntityRelation.relation_props,
                EntityRelation.source_doc_uuid,
            ).filter(
                EntityRelation.review_status.in_(statuses),
                EntityRelation.is_deleted == False,
                EntityRelation.from_entity_id.in_(node_ids),
                EntityRelation.to_entity_id.in_(node_ids),
            )
            if source_doc_uuid:
                rq = rq.filter(EntityRelation.source_doc_uuid == source_doc_uuid)
            return [
                GraphEdge(
                    id=r.id,
                    relation_type=r.relation_type or "",
                    from_entity_id=r.from_entity_id,
                    to_entity_id=r.to_entity_id,
                    review_status=r.review_status or "",
                    relation_props=r.relation_props or {},
                    source_doc_uuid=r.source_doc_uuid or "",
                )
                for r in rq.all()
            ]

        # ══════════════════════════════════════════════════════════════════
        # 遍历模式选择
        # ══════════════════════════════════════════════════════════════════

        if traversal == "bfs":
            # ── BFS 模式：从高连接度种子节点逐跳展开 ─────────────────────

            # Step 1: 获取所有符合过滤条件的实体 ID + 度数
            all_entities_q = (
                session.query(
                    ExtractedEntity.id,
                    func.coalesce(degree_agg.c.degree, 0).label('degree'),
                )
                .outerjoin(degree_agg, ExtractedEntity.id == degree_agg.c.entity_id)
            )
            all_entities_q = _entity_filter(all_entities_q)
            all_entities = {row.id: row.degree for row in all_entities_q.all()}

            if not all_entities:
                return GraphResponse(nodes=[], edges=[], entity_types=[], source_sites=[])

            # Step 2: 种子节点 — top 20 按度数降序
            seeds = sorted(all_entities.items(), key=lambda x: x[1], reverse=True)[:20]
            seed_ids = {s[0] for s in seeds}

            # Step 3: 构建邻接表（仅查匹配实体之间的边）
            all_ids = set(all_entities.keys())
            adj: dict[int, list[int]] = {eid: [] for eid in all_ids}
            rel_rows = session.query(
                EntityRelation.from_entity_id,
                EntityRelation.to_entity_id,
            ).filter(
                EntityRelation.review_status.in_(statuses),
                EntityRelation.is_deleted == False,
                EntityRelation.from_entity_id.in_(all_ids),
                EntityRelation.to_entity_id.in_(all_ids),
            ).all()
            for from_id, to_id in rel_rows:
                adj[from_id].append(to_id)
                adj[to_id].append(from_id)

            # Step 4: BFS 逐跳展开，队列空了就从剩余未访问节点补种子
            visited: list[int] = []
            visited_set: set[int] = set()
            # 按度数降序排列，作为种子选取顺序
            sorted_by_deg = sorted(all_entities.items(), key=lambda x: x[1], reverse=True)
            seed_idx = 0

            while len(visited) < limit and seed_idx < len(sorted_by_deg):
                # 找下一个未访问的最高度节点当新种子
                frontier = deque()
                while seed_idx < len(sorted_by_deg):
                    sid, _ = sorted_by_deg[seed_idx]
                    seed_idx += 1
                    if sid not in visited_set:
                        frontier.append((sid, 0))
                        break

                # 从该种子 BFS 展开
                while frontier and len(visited) < limit:
                    eid, depth = frontier.popleft()
                    if eid in visited_set:
                        continue
                    visited.append(eid)
                    visited_set.add(eid)
                    if depth < max_depth:
                        for neighbor in adj.get(eid, []):
                            if neighbor not in visited_set:
                                frontier.append((neighbor, depth + 1))

            ordered_ids = visited[:limit]

        else:
            # ── degree 模式：按度数降序 ──────────────────────────────────
            q = (
                session.query(ExtractedEntity.id)
                .outerjoin(degree_agg, ExtractedEntity.id == degree_agg.c.entity_id)
            )
            q = _entity_filter(q)
            q = q.order_by(func.coalesce(degree_agg.c.degree, 0).desc())
            ordered_ids = [r.id for r in q.limit(limit).all()]

        # ── 拉取节点 + 边 ────────────────────────────────────────────────
        nodes = _fetch_nodes(ordered_ids)
        edges = _fetch_edges(ordered_ids) if include_relations else []

    return GraphResponse(
        nodes=nodes,
        edges=edges,
        entity_types=sorted(
            t[0] for t in session.query(ExtractedEntity.entity_type.distinct())
            .filter(ExtractedEntity.is_deleted == False, ExtractedEntity.entity_type.isnot(None))
            .all()
            if t[0]
        ),
        source_sites=sorted(
            s[0] for s in session.query(ExtractedEntity.source_site.distinct())
            .filter(ExtractedEntity.is_deleted == False, ExtractedEntity.source_site.isnot(None))
            .all()
            if s[0]
        ),
    )


# ── 仪表盘统计 ───────────────────────────────────────────────────────────

@router.get("/stats", response_model=ReviewStatsResponse)
def get_review_stats():
    """仪表盘统计：按状态/类型/来源站点统计实体和关系数量。"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        # 实体按状态统计
        ent_status = dict(
            session.query(
                ExtractedEntity.review_status,
                func.count(ExtractedEntity.id),
            )
            .filter(
                ExtractedEntity.is_deleted == False,
            )
            .group_by(ExtractedEntity.review_status)
            .all()
        )

        # 关系按状态统计
        rel_status = dict(
            session.query(
                EntityRelation.review_status,
                func.count(EntityRelation.id),
            )
            .filter(
                EntityRelation.is_deleted == False,
            )
            .group_by(EntityRelation.review_status)
            .all()
        )

        def _normalize(status: str) -> str:
            return "approved" if status == "auto_approved" else status

        # 实体按类型统计
        ent_by_type_rows = (
            session.query(
                ExtractedEntity.entity_type,
                ExtractedEntity.review_status,
                func.count(ExtractedEntity.id),
            )
            .filter(
                ExtractedEntity.is_deleted == False,
            )
            .group_by(ExtractedEntity.entity_type, ExtractedEntity.review_status)
            .all()
        )

        by_type: dict[str, StatusCounts] = {}
        for entity_type, status, cnt in ent_by_type_rows:
            key = _normalize(status)
            if key not in ("pending", "approved", "rejected"):
                continue
            if entity_type not in by_type:
                by_type[entity_type] = StatusCounts()
            current = getattr(by_type[entity_type], key)
            setattr(by_type[entity_type], key, current + cnt)

        # 实体按来源站点统计
        ent_by_site_rows = (
            session.query(
                ExtractedEntity.source_site,
                ExtractedEntity.review_status,
                func.count(ExtractedEntity.id),
            )
            .filter(
                ExtractedEntity.is_deleted == False,
            )
            .group_by(ExtractedEntity.source_site, ExtractedEntity.review_status)
            .all()
        )

        by_site: dict[str, StatusCounts] = {}
        for source_site, status, cnt in ent_by_site_rows:
            key = _normalize(status)
            if key not in ("pending", "approved", "rejected"):
                continue
            site = source_site or "unknown"
            if site not in by_site:
                by_site[site] = StatusCounts()
            current = getattr(by_site[site], key)
            setattr(by_site[site], key, current + cnt)

        return ReviewStatsResponse(
            entities=StatusCounts(
                pending=ent_status.get("pending", 0),
                approved=ent_status.get("approved", 0) + ent_status.get("auto_approved", 0),
                rejected=ent_status.get("rejected", 0),
            ),
            relations=StatusCounts(
                pending=rel_status.get("pending", 0),
                approved=rel_status.get("approved", 0) + rel_status.get("auto_approved", 0),
                rejected=rel_status.get("rejected", 0),
            ),
            by_type=by_type,
            by_site=by_site,
        )


# ── 实体/关系元数据编辑 ──────────────────────────────────────────────────

@router.patch("/entities/{entity_id}", response_model=ReviewActionResponse)
def patch_entity(entity_id: int, body: PatchEntityRequest):
    """编辑实体元数据（entity_data 浅合并，不可修改 name/type/status）。"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")

        # 浅合并 entity_data
        existing = dict(row.entity_data or {})
        existing.update(body.entity_data)
        row.entity_data = existing

        # 已推送的实体重置同步状态
        if row.weknora_graph_status == "pushed":
            row.weknora_graph_status = "pending"

        session.commit()

    logger.info(f"[API] 实体元数据更新: entity_id={entity_id}")
    return ReviewActionResponse(success=True, message="实体元数据已更新")


@router.patch("/relations/{relation_id}", response_model=ReviewActionResponse)
def patch_relation(relation_id: int, body: PatchRelationRequest):
    """编辑关系元数据（relation_props 浅合并）。"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        row = session.query(EntityRelation).filter(
            EntityRelation.id == relation_id,
            EntityRelation.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="关系不存在")

        existing = dict(row.relation_props or {})
        existing.update(body.relation_props)
        row.relation_props = existing

        if row.weknora_graph_status == "pushed":
            row.weknora_graph_status = "pending"

        session.commit()

    logger.info(f"[API] 关系元数据更新: relation_id={relation_id}")
    return ReviewActionResponse(success=True, message="关系元数据已更新")


# ── 实体合并工作台 ─────────────────────────────────────────────────────────


@router.get("/search-entities")
def search_entities(q: str = Query("", description="搜索关键词"), limit: int = Query(20, ge=1, le=100)):
    """模糊搜索 entity_dict（按别名或名称），用于合并工作台的实体选择。"""
    try:
        if not q.strip():
            return []
        results = search_entity_dicts(q, limit)
        return [SearchEntityItem(**r) for r in results]
    except Exception as e:
        logger.error(f"[API] 实体搜索失败: q={q} | {e}")
        raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")


@router.get("/compare-entities", response_model=CompareEntitiesResponse)
def compare_entities_api(dict_ids: str = Query("", description="逗号分隔的 dict_id 列表")):
    """对比多个 entity_dict 的 canonical_data，返回并排字段差异。"""
    try:
        ids = [int(x.strip()) for x in dict_ids.split(",") if x.strip()]
        if len(ids) < 2:
            raise HTTPException(status_code=400, detail="至少需要 2 个 dict_id")
        if len(ids) > 10:
            raise HTTPException(status_code=400, detail="最多支持 10 个实体对比")
        result = compare_entities(ids)
        return CompareEntitiesResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 实体对比失败: dict_ids={dict_ids} | {e}")
        raise HTTPException(status_code=500, detail=f"对比失败: {str(e)}")


@router.post("/merge-entities", response_model=MergeEntitiesResponse)
def merge_entities_api(body: MergeEntitiesRequest):
    """执行实体身份合并。后端根据 field_decisions 自动计算合并结果。"""
    try:
        if body.keeper_dict_id in body.absorbed_dict_ids:
            raise HTTPException(status_code=400, detail="keeper 不能出现在 absorbed 列表中")
        result = merge_entities(
            keeper_dict_id=body.keeper_dict_id,
            absorbed_dict_ids=body.absorbed_dict_ids,
            field_decisions=body.field_decisions,
            reviewer=body.reviewer,
            note=body.note,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "合并失败"))
        logger.info(
            f"[API] 实体合并完成: keeper={body.keeper_dict_id} "
            f"absorbed={body.absorbed_dict_ids} merge_log={result.get('merge_log_id')}"
        )
        return MergeEntitiesResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 实体合并失败: {e}")
        raise HTTPException(status_code=500, detail=f"合并失败: {str(e)}")


@router.post("/rollback-merge", response_model=RollbackMergeResponse)
def rollback_merge_api(body: RollbackMergeRequest):
    """回滚实体合并：从 entity_merge_log 快照恢复合并前状态。"""
    try:
        result = rollback_merge(body.merge_log_id, body.reason)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "回滚失败"))
        logger.info(
            f"[API] 合并回滚完成: merge_log={body.merge_log_id} "
            f"restored={result.get('restored_dict_ids')}"
        )
        return RollbackMergeResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] 合并回滚失败: merge_log={body.merge_log_id} | {e}")
        raise HTTPException(status_code=500, detail=f"回滚失败: {str(e)}")


@router.get("/merge-logs", response_model=MergeLogListResponse)
def list_merge_logs(limit: int = Query(50, ge=1, le=200)):
    """查询实体合并日志列表。"""
    try:
        rows = get_merge_logs(limit)
        items = [MergeLogItem(**r) for r in rows]
        return MergeLogListResponse(total=len(items), items=items)
    except Exception as e:
        logger.error(f"[API] 合并日志查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")
