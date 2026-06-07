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
    extraction_model: str = ""
    extracted_at: Optional[str] = None
    extraction_error: str = ""


class ExtractionStatsResponse(BaseModel):
    pending: int
    processing: int
    done: int
    failed: int


# ── Review ─────────────────────────────────────────────────────────────

class DisambigCandidate(BaseModel):
    dict_id: int
    name: str
    similarity_score: float = 0.0
    match_type: str = ""


class DisambigInfo(BaseModel):
    status: str = ""
    candidates: list[DisambigCandidate] = []
    match_step: str = ""


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
    disambig: Optional[DisambigInfo] = None


class EntityListResponse(BaseModel):
    total: int
    items: list[EntityItem]


class RelationItem(BaseModel):
    id: int
    source_doc_uuid: str
    relation_type: str
    from_entity_id: Optional[int]
    from_entity_name: str = ""
    to_entity_id: Optional[int]
    to_entity_name: str = ""
    relation_props: dict
    confidence_score: float
    source_text: str
    review_status: str


class RelationListResponse(BaseModel):
    total: int
    items: list[RelationItem]


class DictCandidateItem(BaseModel):
    """审核页实时查询的 entity_dict 候选项。"""
    dict_id: int
    entity_type: str
    name: str
    aliases: list[str] = []
    canonical_source: str = ""
    is_verified: bool = False
    match_type: str = ""   # "exact_alias" | "vector"
    score: float = 0.0


class DictCandidateListResponse(BaseModel):
    total: int
    items: list[DictCandidateItem]


class ReviewActionResponse(BaseModel):
    success: bool
    message: str = ""


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


class RecrawlResponse(BaseModel):
    success: bool
    uuid: str
    message: str = ""


class BatchRecrawlResponse(BaseModel):
    success: bool
    triggered: int
    skipped: int = 0
    message: str = ""


# ── Worker ─────────────────────────────────────────────────────────────

class WorkerItem(BaseModel):
    worker_id: int
    queue_size: int


class WorkerStatsResponse(BaseModel):
    num_workers: int
    total_pending: int
    workers: list[WorkerItem]


# ── Merge Queue ─────────────────────────────────────────────────────────

class MergeQueueItem(BaseModel):
    id: int
    source_entity_id: int
    target_dict_id: int
    field_name: str
    existing_value: Any = None
    new_value: Any = None
    source_site: str = ""
    resolution: str = ""
    created_at: Optional[str] = None


class MergeQueueListResponse(BaseModel):
    total: int
    items: list[MergeQueueItem]


# ── WeKnora Graph Sync ──────────────────────────────────────────────────

class GraphSyncResponse(BaseModel):
    success: bool
    pushed: int = 0
    failed: int = 0
    message: str = ""


class GraphSyncStatusCount(BaseModel):
    pending: int = 0
    pushed: int = 0
    failed: int = 0

    # entity_dict uses synced instead of pushed
    synced: int = 0


class GraphSyncStatsResponse(BaseModel):
    entity_dict: GraphSyncStatusCount
    entities: GraphSyncStatusCount
    relations: GraphSyncStatusCount


# ── Graph Review ─────────────────────────────────────────────────────────

class GraphNode(BaseModel):
    id: int
    entity_type: str
    entity_name: str
    entity_data: dict = {}
    dict_id: Optional[int] = None
    review_status: str
    source_doc_uuid: str
    source_site: str
    confidence_score: float = 0.0
    is_verified: bool = False


class GraphEdge(BaseModel):
    id: int
    relation_type: str
    from_entity_id: Optional[int]
    to_entity_id: Optional[int]
    review_status: str
    relation_props: dict = {}
    source_doc_uuid: str


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class StatusCounts(BaseModel):
    pending: int = 0
    approved: int = 0
    rejected: int = 0


class ReviewStatsResponse(BaseModel):
    entities: StatusCounts
    relations: StatusCounts
    by_type: dict[str, StatusCounts] = {}
    by_site: dict[str, StatusCounts] = {}


# ── Re-extract ───────────────────────────────────────────────────────────

class ReExtractResponse(BaseModel):
    success: bool
    uuid: str
    message: str = ""
