# -*- coding: utf-8 -*-
"""审核操作接口"""
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.schemas.request import (
    ReviewActionRequest,
    BatchApproveEntitiesRequest,
    BatchRejectEntitiesRequest,
    BatchApproveRelationsRequest,
    BatchRejectRelationsRequest
)
from api.schemas.response import (
    EntityListResponse, EntityItem,
    RelationListResponse, RelationItem,
    ReviewActionResponse,
)
from db.repository import (
    approve_entity, reject_entity,
    approve_relation, reject_relation,
    batch_approve_entities, batch_reject_entities,
    batch_approve_relations, batch_reject_relations,
    log_review_action,
)
from db.session import get_session
from db.models import ExtractedEntity, EntityRelation

router = APIRouter(prefix="/api/review", tags=["review"])


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
    """审核通过单个实体"""
    try:
        approve_entity(entity_id, reviewer=body.reviewer, note=body.note or "")
        logger.info(f"[API] 审核实体通过: id={entity_id}, reviewer={body.reviewer}")
        return ReviewActionResponse(success=True)
    except Exception as e:
        logger.error(f"[API] 审核实体失败: id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/entities/{entity_id}/reject", response_model=ReviewActionResponse)
def reject_entity_api(entity_id: int, body: ReviewActionRequest):
    """审核拒绝单个实体"""
    try:
        reject_entity(entity_id, reviewer=body.reviewer, note=body.note or "")
        logger.info(f"[API] 审核实体拒绝: id={entity_id}, reviewer={body.reviewer}")
        return ReviewActionResponse(success=True)
    except Exception as e:
        logger.error(f"[API] 审核实体失败: id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


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
            q = session.query(EntityRelation).filter(
                EntityRelation.review_status == review_status,
                EntityRelation.is_deleted == False,
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
                    id=r.id,
                    source_doc_uuid=r.source_doc_uuid or "",
                    relation_type=r.relation_type or "",
                    from_entity_id=r.from_entity_id,
                    to_entity_id=r.to_entity_id,
                    relation_props=r.relation_props or {},
                    confidence_score=r.confidence_score or 0.0,
                    source_text=r.source_text or "",
                    review_status=r.review_status or "",
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
    try:
        approve_relation(relation_id, reviewer=body.reviewer, note=body.note or "")
        logger.info(f"[API] 审核关系通过: id={relation_id}, reviewer={body.reviewer}")
        return ReviewActionResponse(success=True)
    except Exception as e:
        logger.error(f"[API] 审核关系失败: id={relation_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")


@router.post("/relations/{relation_id}/reject", response_model=ReviewActionResponse)
def reject_relation_api(relation_id: int, body: ReviewActionRequest):
    """审核拒绝单个关系"""
    try:
        reject_relation(relation_id, reviewer=body.reviewer, note=body.note or "")
        logger.info(f"[API] 审核关系拒绝: id={relation_id}, reviewer={body.reviewer}")
        return ReviewActionResponse(success=True)
    except Exception as e:
        logger.error(f"[API] 审核关系失败: id={relation_id} | {e}")
        raise HTTPException(status_code=500, detail=f"审核失败: {str(e)}")
