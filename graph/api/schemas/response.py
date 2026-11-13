"""响应体 Pydantic 模型"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


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
    updated_at: Optional[str] = None


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
    # 主体是 history 固定版本时的引用（正文来自该版本的清洗正文）
    history_ref: dict | None = None
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
    queued: int
    processing: int
    done: int
    failed: int
    no_entity: int


# ── Review ─────────────────────────────────────────────────────────────

class ObjectReviewEvidence(BaseModel):
    current_review_evidence: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    source_site: str = ""
    source_version_id: Optional[str] = None
    source_quote: Optional[str] = None
    source_char_start: Optional[int] = None
    source_char_end: Optional[int] = None
    first_seen_at: Optional[datetime] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    reviewed_by: str = ""
    reviewed_at: Optional[datetime] = None
    reviewer_note: str = ""
    confidence_reason: str = ""


class EntityReviewEvidence(ObjectReviewEvidence):
    identity_confirmed_by: Optional[str] = None
    identity_confirmed_at: Optional[datetime] = None
    identity_confirmation_basis: Optional[str] = None
    dict_context: Optional[dict] = None


class DisambigCandidate(BaseModel):
    dict_id: int
    name: str = ""
    score: Optional[float] = None
    similarity_score: float = 0.0
    match_type: str = ""


class DisambigInfo(BaseModel):
    status: str = ""
    candidates: list[DisambigCandidate] = []
    match_step: str = ""
    dict_id: Optional[int] = None
    auto_link: bool = False
    confidence: float = 0.0
    reason: str = ""
    rule_version: Optional[str] = None
    model_version: Optional[str] = None
    decision_policy: str = ""
    calibration_status: str = ""
    model_trace: list[dict] = Field(default_factory=list)


class EntityItem(EntityReviewEvidence):
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


class RelationItem(ObjectReviewEvidence):
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

# ── Worker ─────────────────────────────────────────────────────────────

class WorkerItem(BaseModel):
    worker_id: int
    queue_size: int


class WorkerStatsResponse(BaseModel):
    num_workers: int
    total_pending: int
    paused: bool = False
    workers: list[WorkerItem]


# ── Merge Queue ─────────────────────────────────────────────────────────

class MergeSourceInfo(BaseModel):
    entity_id: int = 0
    source_doc_uuid: str = ""
    source_site: str = ""


class MergeQueueConflict(BaseModel):
    merge_id: int
    field_name: str
    existing_value: Any = None
    new_value: Any = None
    source_entity_id: int = 0
    source_site: str = ""
    source_doc_uuid: str = ""
    existing_source_doc_uuid: str = ""
    existing_sources: list[MergeSourceInfo] = []
    new_sources: list[MergeSourceInfo] = []


class MergeQueueGroup(BaseModel):
    dict_id: int
    entity_type: str = ""
    entity_name: str = ""
    canonical_data: dict = {}
    canonical_source: str = ""
    conflicts: list[MergeQueueConflict] = []


class MergeQueueListResponse(BaseModel):
    total_groups: int
    groups: list[MergeQueueGroup]


# ── 身份合并提案 ─────────────────────────────────────────────────────────

class IdentityMergeProposalItem(BaseModel):
    id: int
    source_entity_id: int
    target_dict_id: int
    source_entity_snapshot: dict = {}
    target_dict_snapshot: dict = {}
    agent_run_id: str = ""
    agent_reason: str = ""
    agent_confidence: float = 0.0
    status: str = "pending"
    reviewer: str = ""
    review_note: str = ""
    reviewed_at: str = ""
    created_at: str = ""


class IdentityMergeProposalListResponse(BaseModel):
    total: int = 0
    items: list[IdentityMergeProposalItem] = []


class IdentityMergeProposalResolveRequest(BaseModel):
    reviewer: str = "审核员"
    note: str = ""


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

class GraphNode(EntityReviewEvidence):
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


class GraphEdge(ObjectReviewEvidence):
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
    entity_types: list[str] = []
    source_sites: list[str] = []


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


# ── Entity Merge Workbench ──────────────────────────────────────────────

class SearchEntityItem(BaseModel):
    dict_id: int
    entity_type: str
    entity_name: str
    canonical_source: str = ""
    aliases: list[str] = []
    extracted_entities_count: int = 0


class CompareEntityInfo(BaseModel):
    dict_id: int
    entity_type: str
    entity_name: str
    canonical_data: dict = {}
    canonical_source: str = ""
    aliases: list[str] = []
    extracted_entities_count: int = 0


class FieldDiffItem(BaseModel):
    field: str
    values: dict[str, Any] = {}  # dict_id → value
    conflict: bool


class CompareEntitiesResponse(BaseModel):
    entities: list[CompareEntityInfo]
    field_diff: list[FieldDiffItem]


class MergeEntitiesResponse(BaseModel):
    success: bool
    merge_log_id: int = 0
    keeper_dict_id: int
    merged_count: int = 0
    migrated_extractions: int = 0
    migrated_aliases: int = 0
    migrated_relations: int = 0


class RollbackMergeResponse(BaseModel):
    success: bool
    merge_log_id: int = 0
    restored_dict_ids: list[int] = []
    restored_extractions: int = 0
    restored_aliases: int = 0
    restored_relations: int = 0


class MergeLogItem(BaseModel):
    id: int
    keeper_dict_id: int
    absorbed_dict_ids: list[int] = []
    merge_type: str = ""
    reviewer: str = ""
    note: str = ""
    is_rolled_back: bool = False
    created_at: Optional[str] = None


class MergeLogListResponse(BaseModel):
    total: int
    items: list[MergeLogItem]


# ── 流程总览 ─────────────────────────────────────────────────────────────

class PipelineStage(BaseModel):
    """流水线单个阶段：总量 + 各状态分布 + 各状态站点分布 + 消歧 match_step 分布 + 字典现状（可选）。"""
    key: str
    label: str
    total: int
    by_status: dict[str, int]
    by_site: dict[str, dict[str, int]] = Field(default_factory=dict)
    by_match_step: dict[str, int] = Field(default_factory=dict)
    by_dict: dict[str, int] = Field(default_factory=dict)


class PipelineOverviewResponse(BaseModel):
    stages: list[PipelineStage]


class EntityReviewStatsResponse(BaseModel):
    """实体审核存量统计。"""
    pending_entities: int
    reviewed_entities: int
