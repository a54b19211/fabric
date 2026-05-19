"""审核操作接口"""
from fastapi import APIRouter, HTTPException, Query

from api.schemas.request import ReviewActionRequest
from api.schemas.response import (
    EntityListResponse, EntityItem,
    RelationListResponse, RelationItem,
    ReviewActionResponse,
)
from db.repository import (
    approve_entity, reject_entity,
    approve_relation, reject_relation,
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
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")
        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")
        return EntityItem(
            id=row.id,
            source_doc_uuid=row.source_doc_uuid,
            entity_type=row.entity_type,
            entity_name=row.entity_name,
            entity_data=row.entity_data,
            confidence_score=row.confidence_score,
            confidence_reason=row.confidence_reason,
            source_text=row.source_text,
            review_status=row.review_status,
            dict_id=row.dict_id,
        )


@router.post("/entities/{entity_id}/approve", response_model=ReviewActionResponse)
def approve_entity_api(entity_id: int, body: ReviewActionRequest):
    approve_entity(entity_id, reviewer=body.reviewer, note=body.note)
    return ReviewActionResponse(success=True)


@router.post("/entities/{entity_id}/reject", response_model=ReviewActionResponse)
def reject_entity_api(entity_id: int, body: ReviewActionRequest):
    reject_entity(entity_id, reviewer=body.reviewer, note=body.note)
    return ReviewActionResponse(success=True)


# ── 关系 ───────────────────────────────────────────────────────────────

@router.get("/relations", response_model=RelationListResponse)
def list_relations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    source_site: str = Query(""),
    source_doc_uuid: str = Query(""),
    review_status: str = Query("pending"),
):
    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        q = session.query(EntityRelation).filter(
            EntityRelation.review_status == review_status,
            EntityRelation.is_deleted == False,
        )
        if source_site:
            q = q.filter(EntityRelation.source_site == source_site)
        if source_doc_uuid:
            q = q.filter(EntityRelation.source_doc_uuid == source_doc_uuid)

        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()

        items = [
            RelationItem(
                id=r.id,
                source_doc_uuid=r.source_doc_uuid,
                relation_type=r.relation_type,
                from_entity_id=r.from_entity_id,
                to_entity_id=r.to_entity_id,
                relation_props=r.relation_props,
                confidence_score=r.confidence_score,
                source_text=r.source_text,
                review_status=r.review_status,
            )
            for r in rows
        ]
        return RelationListResponse(total=total, items=items)


@router.post("/relations/{relation_id}/approve", response_model=ReviewActionResponse)
def approve_relation_api(relation_id: int, body: ReviewActionRequest):
    approve_relation(relation_id, reviewer=body.reviewer, note=body.note)
    return ReviewActionResponse(success=True)


@router.post("/relations/{relation_id}/reject", response_model=ReviewActionResponse)
def reject_relation_api(relation_id: int, body: ReviewActionRequest):
    reject_relation(relation_id, reviewer=body.reviewer, note=body.note)
    return ReviewActionResponse(success=True)
