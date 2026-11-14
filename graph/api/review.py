# -*- coding: utf-8 -*-
"""审核操作接口"""
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy import false, func, or_
from sqlalchemy.orm import aliased

from graph.api.review_auth import optional_review_principal, require_review_principal
from graph.api.schemas.request import (
    BatchApproveEntitiesRequest,
    BatchApproveRelationsRequest,
    BatchRejectEntitiesRequest,
    BatchRejectRelationsRequest,
    BatchResolveMergeRequest,
    IdentityProposalDecisionRequest,
    MergeEntitiesRequest,
    PatchEntityRequest,
    PatchRelationRequest,
    PrimeKGMappingRequest,
    ResolveMergeRequest,
    ReviewActionRequest,
    RollbackMergeRequest,
)
from graph.api.schemas.response import (
    CompareEntitiesResponse,
    DictCandidateItem,
    DictCandidateListResponse,
    DisambigInfo,
    EntityItem,
    EntityListResponse,
    GraphEdge,
    GraphNode,
    GraphResponse,
    MergeEntitiesResponse,
    MergeQueueListResponse,
    RelationItem,
    RelationListResponse,
    ReviewActionResponse,
    ReviewStatsResponse,
    RollbackMergeResponse,
    SearchEntityItem,
    StatusCounts,
)
from graph.db.identity import get_current_review_evidence, get_objects_evidence
from graph.db.models import EntityDict, EntityRelation, EntitySourceEvidence, ExtractedEntity
from graph.db.repository import (
    approve_entity_with_alias_writeback,
    approve_relation,
    batch_approve_entities,
    batch_approve_relations,
    batch_reject_entities,
    batch_reject_relations,
    batch_resolve_merges,
    compare_entities,
    create_primekg_mapping_proposal,
    find_candidate_dicts_for_review,
    get_identity_merge_proposals,
    get_merge_logs,
    get_object_evidence,
    get_pending_merges,
    get_primekg_mapping_proposals,
    merge_entities,
    reject_entity,
    reject_relation,
    resolve_identity_merge_proposal,
    resolve_merge,
    resolve_primekg_mapping_proposal,
    rollback_merge,
    search_entity_dicts,
)
from graph.db.session import get_session
from store.record_models import FabricError

router = APIRouter(prefix="/api/review", tags=["review"])


def _extract_disambig(entity_data: dict | None) -> DisambigInfo | None:
    raw = (entity_data or {}).get('_disambig')
    if not raw:
        return None
    values = dict(raw)
    values['candidates'] = [{**candidate,
        'similarity_score': candidate.get('similarity_score', candidate.get('score', 0.0))}
        for candidate in raw.get('candidates') or []]
    return DisambigInfo(**values)


def _source_review_fields(row):
    values = {key: getattr(row, key, None) for key in (
        'source_version_id', 'source_quote', 'source_char_start', 'source_char_end',
        'first_seen_at', 'valid_from', 'valid_to', 'reviewed_at')}
    values.update({key: getattr(row, key, '') or '' for key in ('source_site', 'reviewed_by', 'reviewer_note')})
    return values


def _mention_review_fields(row):
    return {key: getattr(row, key, None) for key in (
        'identity_confirmed_by', 'identity_confirmed_at', 'identity_confirmation_basis')}


def _dictionary_context(row):
    if row is None:
        return None
    return {key: getattr(row, key) for key in ('id', 'is_verified', 'verified_by', 'verified_at', 'verification_basis')}


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
            ExtractedEntity.is_deleted == false(),
        )
        if entity_type:
            q = q.filter(ExtractedEntity.entity_type == entity_type)
        if source_site:
            q = q.filter(ExtractedEntity.source_site == source_site)
        if source_doc_uuid:
            recorded_source = session.query(EntitySourceEvidence.id).filter(
                EntitySourceEvidence.object_type == 'entity', EntitySourceEvidence.object_id == ExtractedEntity.id,
                EntitySourceEvidence.source_doc_uuid == source_doc_uuid).exists()
            q = q.filter(or_(ExtractedEntity.source_doc_uuid == source_doc_uuid, recorded_source))

        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()

        dict_ids = {row.dict_id for row in rows if row.dict_id}
        dictionaries = {row.id: row for row in session.query(EntityDict).filter(EntityDict.id.in_(dict_ids)).all()} if dict_ids else {}
        evidence_map = get_objects_evidence('entity', [row.id for row in rows], session=session)
        review_evidence_map = get_current_review_evidence('entity', [row.id for row in rows], session=session)
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
                evidence=evidence_map.get(r.id, []),
                current_review_evidence=review_evidence_map.get(r.id, []),
                disambig=_extract_disambig(r.entity_data),
                **_source_review_fields(r), **_mention_review_fields(r),
                dict_context=_dictionary_context(dictionaries.get(r.dict_id)),
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
                ExtractedEntity.is_deleted == false(),
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
                evidence=get_object_evidence('entity', row.id, session=session),
                current_review_evidence=get_current_review_evidence('entity', [row.id], session=session).get(row.id, []),
                disambig=_extract_disambig(row.entity_data),
                **_source_review_fields(row), **_mention_review_fields(row),
                dict_context=_dictionary_context(session.get(EntityDict, row.dict_id) if row.dict_id else None),
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
def approve_entity_api(entity_id: int, body: ReviewActionRequest, principal=Depends(optional_review_principal)):
    """审核通过单个实体（含 alias 回写 + embedding 生成）。"""
    if body.confirm_identity and principal is None:
        raise HTTPException(status_code=403, detail='A trusted review token is required to confirm identity')
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == false(),
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")

    try:
        approve_entity_with_alias_writeback(
            entity_id, reviewer=body.reviewer, note=body.note or "",
            dict_id=body.dict_id,
            force_new=body.force_new,
            confirm_identity=body.confirm_identity, basis=body.basis,
            evidence=body.evidence or None, review_context=principal,
        )
        logger.info(f"[API] 审核实体通过: id={entity_id}, reviewer={body.reviewer}, dict_id={body.dict_id}, force_new={body.force_new}")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except FabricError as exc:
        raise HTTPException(status_code=exc.status, detail={'code': exc.code, 'message': exc.message})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
            ExtractedEntity.is_deleted == false(),
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
            ExtractedEntity.is_deleted == false(),
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
                    EntityRelation.is_deleted == false(),
                )
            )

            # 动态筛选条件
            if source_site:
                q = q.filter(EntityRelation.source_site == source_site)
            if source_doc_uuid:
                recorded_source = session.query(EntitySourceEvidence.id).filter(
                    EntitySourceEvidence.object_type == 'relation', EntitySourceEvidence.object_id == EntityRelation.id,
                    EntitySourceEvidence.source_doc_uuid == source_doc_uuid).exists()
                q = q.filter(or_(EntityRelation.source_doc_uuid == source_doc_uuid, recorded_source))

            total = q.count()
            rows = q.offset((page - 1) * page_size).limit(page_size).all()

            evidence_map = get_objects_evidence('relation', [row.EntityRelation.id for row in rows], session=session)
            review_evidence_map = get_current_review_evidence('relation', [row.EntityRelation.id for row in rows], session=session)
            items = [
                RelationItem(
                    evidence=evidence_map.get(r.EntityRelation.id, []),
                    current_review_evidence=review_evidence_map.get(r.EntityRelation.id, []),
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
                    **_source_review_fields(r.EntityRelation),
                )
                for r in rows
            ]

            logger.info(f"[API] 关系列表查询: page={page}, total={total}")
            return RelationListResponse(total=total, items=items)

        except Exception as e:
            logger.error(f"[API] 关系列表查询失败: {e}")
            raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.get('/relations/{relation_id}', response_model=RelationItem)
def get_relation(relation_id: int):
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail='Database is not configured')
        row = session.get(EntityRelation, relation_id)
        if row is None or row.is_deleted:
            raise HTTPException(status_code=404, detail='Relation does not exist')
        return RelationItem(current_review_evidence=get_current_review_evidence('relation', [row.id], session=session).get(row.id, []),
                            evidence=get_object_evidence('relation', row.id, session=session), id=row.id, source_doc_uuid=row.source_doc_uuid, relation_type=row.relation_type,
                            from_entity_id=row.from_entity_id, to_entity_id=row.to_entity_id,
                            relation_props=row.relation_props or {}, confidence_score=row.confidence_score or 0.0,
                            source_text=row.source_text or '', review_status=row.review_status,
                            **_source_review_fields(row))


@router.post("/relations/batch/approve", response_model=ReviewActionResponse)
def batch_approve_relations_api(body: BatchApproveRelationsRequest, principal=Depends(require_review_principal)):
    """批量审核通过关系"""
    try:
        result = batch_approve_relations(
            source_doc_uuid=body.source_doc_uuid or "",
            source_site=body.source_site or "",
            reviewer=principal.subject,
            review_context=principal,
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
def approve_relation_api(relation_id: int, body: ReviewActionRequest, principal=Depends(require_review_principal)):
    """审核通过单个关系"""
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        row = session.query(EntityRelation).filter(
            EntityRelation.id == relation_id,
            EntityRelation.is_deleted == false(),
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="关系不存在")

    try:
        approve_relation(relation_id, reviewer=principal.subject, note=body.note or "", evidence=body.evidence or None, review_context=principal)
        logger.info(f"[API] 审核关系通过: id={relation_id}, reviewer={body.reviewer}")
        return ReviewActionResponse(success=True)
    except HTTPException:
        raise
    except FabricError as exc:
        raise HTTPException(status_code=exc.status, detail={'code': exc.code, 'message': exc.message})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
            EntityRelation.is_deleted == false(),
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
def resolve_merge_api(merge_id: int, body: ResolveMergeRequest, principal=Depends(require_review_principal)):
    """解决单个合并冲突。resolution: accept_new | keep_existing | custom"""
    try:
        resolve_merge(merge_id, body.resolution, principal.subject, body.resolved_value, review_context=principal)
        logger.info(f"[API] 合并冲突解决: merge_id={merge_id} resolution={body.resolution}")
        return ReviewActionResponse(success=True)
    except Exception as e:
        logger.error(f"[API] 合并冲突解决失败: merge_id={merge_id} | {e}")
        raise HTTPException(status_code=500, detail=f"解决失败: {str(e)}")


@router.post("/merge-queue/resolve-batch", response_model=ReviewActionResponse)
def batch_resolve_merge_api(body: BatchResolveMergeRequest, principal=Depends(require_review_principal)):
    """批量解决合并冲突：一次请求解决同 dict_id 下所有冲突字段。"""
    try:
        result = batch_resolve_merges(body.decisions, principal.subject, review_context=principal)
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
    entity_filters = (statuses, type_list, source_site, source_doc_uuid)

    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        degree_agg = _graph_degree_subquery(session, statuses)
        if traversal == "bfs":
            ordered_ids = _graph_bfs_ids(session, degree_agg, entity_filters, limit, max_depth)
            if ordered_ids is None:
                return GraphResponse(nodes=[], edges=[], entity_types=[], source_sites=[])
        else:
            ordered_ids = _graph_degree_ids(session, degree_agg, entity_filters, limit)
        nodes = _graph_nodes(session, ordered_ids)
        edges = _graph_edges(session, ordered_ids, statuses, source_doc_uuid) if include_relations else []

    return GraphResponse(
        nodes=nodes,
        edges=edges,
        entity_types=_graph_distinct(session, ExtractedEntity.entity_type),
        source_sites=_graph_distinct(session, ExtractedEntity.source_site),
    )


# The steps of get_review_graph. A relation counts towards a node's degree, and joins two nodes, only when it
# is in one of the requested review statuses and not deleted.

def _graph_degree_subquery(session, statuses):
    """Per entity ID, the number of relation ends at it."""
    from_deg = (
        session.query(
            EntityRelation.from_entity_id.label('entity_id'),
            func.count('*').label('degree'),
        )
        .filter(
            EntityRelation.review_status.in_(statuses),
            EntityRelation.is_deleted == false(),
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
            EntityRelation.is_deleted == false(),
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


def _graph_entity_filter(q, entity_filters):
    statuses, type_list, source_site, source_doc_uuid = entity_filters
    q = q.filter(
        ExtractedEntity.review_status.in_(statuses),
        ExtractedEntity.is_deleted == false(),
    )
    if type_list:
        q = q.filter(ExtractedEntity.entity_type.in_(type_list))
    if source_site:
        q = q.filter(ExtractedEntity.source_site == source_site)
    if source_doc_uuid:
        q = q.filter(ExtractedEntity.source_doc_uuid == source_doc_uuid)
    return q


def _graph_bfs_ids(session, degree_agg, entity_filters, limit, max_depth):
    """BFS mode: up to `limit` entity IDs expanded hop by hop from the highest-degree seeds; None when no
    entity matches the filters."""
    all_entities_q = (
        session.query(
            ExtractedEntity.id,
            func.coalesce(degree_agg.c.degree, 0).label('degree'),
        )
        .outerjoin(degree_agg, ExtractedEntity.id == degree_agg.c.entity_id)
    )
    all_entities_q = _graph_entity_filter(all_entities_q, entity_filters)
    all_entities = {row.id: row.degree for row in all_entities_q.all()}
    if not all_entities:
        return None
    adj = _graph_adjacency(session, entity_filters[0], set(all_entities.keys()))
    return _graph_bfs_visit(all_entities, adj, limit, max_depth)


def _graph_adjacency(session, statuses, all_ids):
    """The adjacency lists of the matching entities, over the relations between them only."""
    adj: dict[int, list[int]] = {eid: [] for eid in all_ids}
    rel_rows = session.query(
        EntityRelation.from_entity_id,
        EntityRelation.to_entity_id,
    ).filter(
        EntityRelation.review_status.in_(statuses),
        EntityRelation.is_deleted == false(),
        EntityRelation.from_entity_id.in_(all_ids),
        EntityRelation.to_entity_id.in_(all_ids),
    ).all()
    for from_id, to_id in rel_rows:
        adj[from_id].append(to_id)
        adj[to_id].append(from_id)
    return adj


def _graph_bfs_visit(all_entities, adj, limit, max_depth):
    """BFS hop by hop; when the queue runs dry the highest-degree unvisited entity is the next seed."""
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

    return visited[:limit]


def _graph_degree_ids(session, degree_agg, entity_filters, limit):
    """Degree mode: up to `limit` matching entity IDs by degree, highest first."""
    q = (
        session.query(ExtractedEntity.id)
        .outerjoin(degree_agg, ExtractedEntity.id == degree_agg.c.entity_id)
    )
    q = _graph_entity_filter(q, entity_filters)
    q = q.order_by(func.coalesce(degree_agg.c.degree, 0).desc())
    return [r.id for r in q.limit(limit).all()]


def _graph_node_columns():
    return (
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
        ExtractedEntity.source_version_id, ExtractedEntity.source_quote,
        ExtractedEntity.source_char_start, ExtractedEntity.source_char_end,
        ExtractedEntity.first_seen_at, ExtractedEntity.valid_from, ExtractedEntity.valid_to,
        ExtractedEntity.reviewed_by, ExtractedEntity.reviewed_at, ExtractedEntity.reviewer_note,
        ExtractedEntity.identity_confirmed_by, ExtractedEntity.identity_confirmed_at,
        ExtractedEntity.identity_confirmation_basis,
        EntityDict.verified_by.label('dict_verified_by'), EntityDict.verified_at.label('dict_verified_at'),
        EntityDict.verification_basis.label('dict_verification_basis'),
    )


def _graph_nodes(session, entity_ids: list[int]) -> list[GraphNode]:
    """The nodes of the given entity IDs, in that order, with their evidence and dictionary context."""
    rows = (
        session.query(*_graph_node_columns())
        .outerjoin(EntityDict, ExtractedEntity.dict_id == EntityDict.id)
        .filter(ExtractedEntity.id.in_(entity_ids))
        .all()
    )
    evidence_map = get_objects_evidence('entity', entity_ids, session=session)
    review_evidence_map = get_current_review_evidence('entity', entity_ids, session=session)
    row_map = {r.id: r for r in rows}
    return [
        GraphNode(
            evidence=evidence_map.get(eid, []),
            current_review_evidence=review_evidence_map.get(eid, []),
            id=row_map[eid].id,
            entity_type=row_map[eid].entity_type or "",
            entity_name=row_map[eid].entity_name or "",
            entity_data=row_map[eid].entity_data or {},
            dict_id=row_map[eid].dict_id,
            review_status=row_map[eid].review_status or "",
            source_doc_uuid=row_map[eid].source_doc_uuid or "",
            confidence_score=row_map[eid].confidence_score or 0.0,
            is_verified=row_map[eid].is_verified if row_map[eid].is_verified is not None else False,
            **_source_review_fields(row_map[eid]), **_mention_review_fields(row_map[eid]),
            dict_context={'id': row_map[eid].dict_id, 'is_verified': row_map[eid].is_verified,
                          'verified_by': row_map[eid].dict_verified_by, 'verified_at': row_map[eid].dict_verified_at,
                          'verification_basis': row_map[eid].dict_verification_basis} if row_map[eid].dict_id else None,
        )
        for eid in entity_ids
    ]


def _graph_edges(session, node_ids: list[int], statuses, source_doc_uuid) -> list[GraphEdge]:
    """The relations between the given nodes, with their evidence."""
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
        EntityRelation.source_site, EntityRelation.source_version_id, EntityRelation.source_quote,
        EntityRelation.source_char_start, EntityRelation.source_char_end,
        EntityRelation.first_seen_at, EntityRelation.valid_from, EntityRelation.valid_to,
        EntityRelation.reviewed_by, EntityRelation.reviewed_at, EntityRelation.reviewer_note,
    ).filter(
        EntityRelation.review_status.in_(statuses),
        EntityRelation.is_deleted == false(),
        EntityRelation.from_entity_id.in_(node_ids),
        EntityRelation.to_entity_id.in_(node_ids),
    )
    if source_doc_uuid:
        rq = rq.filter(EntityRelation.source_doc_uuid == source_doc_uuid)
    rows = rq.all()
    evidence_map = get_objects_evidence('relation', [row.id for row in rows], session=session)
    review_evidence_map = get_current_review_evidence('relation', [row.id for row in rows], session=session)
    return [
        GraphEdge(
            evidence=evidence_map.get(r.id, []),
            current_review_evidence=review_evidence_map.get(r.id, []),
            id=r.id,
            relation_type=r.relation_type or "",
            from_entity_id=r.from_entity_id,
            to_entity_id=r.to_entity_id,
            review_status=r.review_status or "",
            relation_props=r.relation_props or {},
            source_doc_uuid=r.source_doc_uuid or "",
            **_source_review_fields(r),
        )
        for r in rows
    ]


def _graph_distinct(session, column):
    """The sorted distinct non-empty values of an entity column over the entities not deleted."""
    return sorted(
        v[0] for v in session.query(column.distinct())
        .filter(ExtractedEntity.is_deleted == false(), column.isnot(None))
        .all()
        if v[0]
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
                ExtractedEntity.is_deleted == false(),
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
                EntityRelation.is_deleted == false(),
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
                ExtractedEntity.is_deleted == false(),
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
                ExtractedEntity.is_deleted == false(),
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
            ExtractedEntity.is_deleted == false(),
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")

        # 浅合并 entity_data
        existing = dict(row.entity_data or {})
        existing.update(body.entity_data)
        row.entity_data = existing
        from graph.db.identity import clear_entity_confirmation
        clear_entity_confirmation(row)

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
            EntityRelation.is_deleted == false(),
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="关系不存在")

        existing = dict(row.relation_props or {})
        existing.update(body.relation_props)
        row.relation_props = existing
        row.review_status = "pending"
        row.reviewed_by = ""
        row.reviewed_at = None

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
def merge_entities_api(body: MergeEntitiesRequest, principal=Depends(require_review_principal)):
    """执行实体身份合并。后端根据 field_decisions 自动计算合并结果。"""
    try:
        if body.keeper_dict_id in body.absorbed_dict_ids:
            raise HTTPException(status_code=400, detail="keeper 不能出现在 absorbed 列表中")
        result = merge_entities(
            keeper_dict_id=body.keeper_dict_id,
            absorbed_dict_ids=body.absorbed_dict_ids,
            field_decisions=body.field_decisions,
            reviewer=principal.subject,
            review_context=principal,
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[API] 实体合并失败: {e}")
        raise HTTPException(status_code=500, detail=f"合并失败: {str(e)}")


@router.post("/rollback-merge", response_model=RollbackMergeResponse)
def rollback_merge_api(body: RollbackMergeRequest, principal=Depends(require_review_principal)):
    """回滚实体合并：从 entity_merge_log 快照恢复合并前状态。"""
    try:
        result = rollback_merge(body.merge_log_id, body.reason, review_context=principal)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "回滚失败"))
        logger.info(
            f"[API] 合并回滚完成: merge_log={body.merge_log_id} "
            f"restored={result.get('restored_dict_ids')}"
        )
        return RollbackMergeResponse(**result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[API] 合并回滚失败: merge_log={body.merge_log_id} | {e}")
        raise HTTPException(status_code=500, detail=f"回滚失败: {str(e)}")


@router.get('/merge-logs')
def list_merge_logs(limit: int = Query(50, ge=1, le=500), dict_ids: str = Query(''),
                    include_rolled_back: bool = Query(True)):
    try:
        selected = [int(value.strip()) for value in dict_ids.split(',') if value.strip()] if dict_ids else None
        if selected is not None and len(selected) > 500:
            raise ValueError('At most 500 dictionary IDs per log lookup')
        items = get_merge_logs(limit=limit, include_rolled_back=include_rolled_back, dict_ids=selected)
        return {'total': len(items), 'items': items}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get('/entity-link-proposals')
@router.get('/identity-merge-proposals')
def list_identity_merge_proposals(status: str = Query('pending'), limit: int = Query(100, ge=1, le=500)):
    items = get_identity_merge_proposals(status=status, limit=limit)
    return {'total': len(items), 'items': items}


@router.post('/entity-link-proposals/{proposal_id}/accept', response_model=ReviewActionResponse)
@router.post('/identity-merge-proposals/{proposal_id}/approve', response_model=ReviewActionResponse)
def approve_identity_merge_proposal(proposal_id: int, body: IdentityProposalDecisionRequest,
                                    principal=Depends(require_review_principal)):
    try:
        resolve_identity_merge_proposal(proposal_id, 'approved', principal.subject, body.note, review_context=principal)
        return ReviewActionResponse(success=True, message='Identity link accepted; content review is unchanged')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post('/entity-link-proposals/{proposal_id}/reject', response_model=ReviewActionResponse)
@router.post('/identity-merge-proposals/{proposal_id}/reject', response_model=ReviewActionResponse)
def reject_identity_merge_proposal(proposal_id: int, body: IdentityProposalDecisionRequest,
                                   principal=Depends(require_review_principal)):
    try:
        resolve_identity_merge_proposal(proposal_id, 'rejected', principal.subject, body.note, review_context=principal)
        return ReviewActionResponse(success=True, message='Proposal rejected')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post('/entity-link-proposals/{proposal_id}/withdraw', response_model=ReviewActionResponse)
@router.post('/identity-merge-proposals/{proposal_id}/withdraw', response_model=ReviewActionResponse)
def withdraw_identity_merge_proposal(proposal_id: int, body: IdentityProposalDecisionRequest,
                                     principal=Depends(require_review_principal)):
    try:
        resolve_identity_merge_proposal(proposal_id, 'withdrawn', principal.subject, body.note, review_context=principal)
        return ReviewActionResponse(success=True, message='Identity link restored from merge log')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get('/entities/{entity_id}/evidence')
def entity_evidence(entity_id: int):
    return {'items': get_object_evidence('entity', entity_id)}


@router.get('/relations/{relation_id}/evidence')
def relation_evidence(relation_id: int):
    return {'items': get_object_evidence('relation', relation_id)}


@router.post('/primekg-mapping-proposals')
def propose_primekg_mapping(body: PrimeKGMappingRequest):
    try:
        return {'id': create_primekg_mapping_proposal(**body.model_dump()), 'status': 'pending'}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get('/primekg-mapping-proposals')
def list_primekg_mapping_proposals(status: str = Query('pending'), dict_id: int | None = None,
                                   limit: int = Query(100, ge=1, le=500)):
    return {'items': get_primekg_mapping_proposals(status=status, dict_id=dict_id, limit=limit)}


@router.post('/primekg-mapping-proposals/{proposal_id}/{decision}', response_model=ReviewActionResponse)
def decide_primekg_mapping(proposal_id: int, decision: str, body: IdentityProposalDecisionRequest,
                           principal=Depends(require_review_principal)):
    status = {'accept': 'approved', 'reject': 'rejected', 'withdraw': 'withdrawn'}.get(decision)
    if status is None:
        raise HTTPException(status_code=400, detail='Unknown mapping decision')
    try:
        resolve_primekg_mapping_proposal(proposal_id, status, principal.subject, body.note, review_context=principal)
        return ReviewActionResponse(success=True)
    except FabricError as exc:
        raise HTTPException(status_code=exc.status, detail={'code': exc.code, 'message': exc.message})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
