"""响应体 Pydantic 模型"""
from typing import Any, Optional
from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str
    code: int


# ── Documents ──────────────────────────────────────────────────────────

class DocumentItem(BaseModel):
    id: int
    uuid: str
    title: str
    source_site: str
    publish_date: str
    doc_status: str
    confidence_score: float
    extraction_status: str
    weknora_sync_status: str


class DocumentListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[DocumentItem]


class AttachmentItem(BaseModel):
    file_name: str
    file_url: str
    local_path: str = ""


class DocumentDetailResponse(BaseModel):
    id: int
    uuid: str
    title: str
    raw_content: str
    source_site: str
    publish_date: str
    doc_status: str
    confidence_score: float
    parse_method: str
    doc_type: str
    language: str
    extra: dict = {}
    attachments: list[AttachmentItem] = []
    extraction_status: str
    extraction_model: str = ""
    extraction_error: str = ""
    extracted_at: Optional[str] = None


# ── Extraction ─────────────────────────────────────────────────────────

class ExtractionTriggerResponse(BaseModel):
    triggered: int
    message: str


class ExtractionStatusResponse(BaseModel):
    uuid: str
    extraction_status: str
    extraction_model: str
    extracted_at: Optional[str]
    extraction_error: str


class ExtractionStatsResponse(BaseModel):
    pending: int
    processing: int
    done: int
    failed: int


# ── Review ─────────────────────────────────────────────────────────────

class EntityItem(BaseModel):
    id: int
    source_doc_uuid: str
    entity_type: str
    entity_name: str
    entity_data: dict
    confidence_score: float
    confidence_reason: str
    source_text: str
    review_status: str
    dict_id: Optional[int] = None


class EntityListResponse(BaseModel):
    total: int
    items: list[EntityItem]


class RelationItem(BaseModel):
    id: int
    source_doc_uuid: str
    relation_type: str
    from_entity_id: Optional[int]
    to_entity_id: Optional[int]
    relation_props: dict
    confidence_score: float
    source_text: str
    review_status: str


class RelationListResponse(BaseModel):
    total: int
    items: list[RelationItem]


class ReviewActionResponse(BaseModel):
    success: bool


# ── Neo4j ──────────────────────────────────────────────────────────────

class Neo4jSyncResponse(BaseModel):
    pushed: int
    failed: int
    message: str


class Neo4jRetryResponse(BaseModel):
    success: bool
    error: str = ""


class Neo4jStatusCount(BaseModel):
    pending: int
    pushed: int
    failed: int


class Neo4jStatsResponse(BaseModel):
    entities: Neo4jStatusCount
    relations: Neo4jStatusCount


class FailedEntityItem(BaseModel):
    id: int
    entity_name: str
    entity_type: str
    neo4j_error: str
    neo4j_pushed_at: Optional[str]


class FailedRelationItem(BaseModel):
    id: int
    relation_type: str
    from_entity_id: Optional[int]
    to_entity_id: Optional[int]
    neo4j_error: str
    neo4j_pushed_at: Optional[str]


class Neo4jFailedResponse(BaseModel):
    entities: list[FailedEntityItem]
    relations: list[FailedRelationItem]


# ── WeKnora ────────────────────────────────────────────────────────────

class WeknoraPendingItem(BaseModel):
    uuid: str
    title: str
    source_site: str
    publish_date: str


class WeknoraPendingResponse(BaseModel):
    total: int
    items: list[WeknoraPendingItem]


class WeknoraSyncResponse(BaseModel):
    success: bool
    message: str


class WeknoraBatchResponse(BaseModel):
    triggered: int


class WeknoraStatsResponse(BaseModel):
    pending: int
    synced: int
    failed: int
