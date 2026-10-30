"""请求体 Pydantic 模型"""
from typing import Optional
from pydantic import BaseModel, Field, validator


class ExtractionTriggerRequest(BaseModel):
    uuids: list[str] = Field(default_factory=list)
    source_site: Optional[str] = Field(default=None, description="按来源站筛选 pending 文档")
    doc_type: Optional[str] = Field(default=None, description="按文档类型筛选")
    limit: int = Field(default=10, ge=1, le=5000, description="单次最多篇数")


class PendingExtractRequest(BaseModel):
    """按数量批量抽取文档（纯数量驱动，不传 UUID 列表），status 支持 pending / failed。"""
    count: int = Field(default=100, ge=1, le=5000, description="抽取数量")
    source_site: Optional[str] = Field(default=None, description="按来源站筛选")
    doc_type: Optional[str] = Field(default=None, description="按文档类型筛选")
    status: str = Field(default="pending", description="抽取状态筛选：pending / failed")


class ReviewActionRequest(BaseModel):
    reviewer: str
    note: str = ""
    dict_id: Optional[int] = None  # 指定链接到的 entity_dict id，None=保持消歧结果
    force_new: bool = False  # 以新实体创建，忽略 dict_id
    confirm_identity: bool = False
    basis: str = ""
    evidence: list[dict] = Field(default_factory=list)


class BatchApproveEntitiesRequest(BaseModel):
    reviewer: str
    note: str = ""
    source_doc_uuid: str = ""
    entity_type: str = ""
    source_site: str = ""
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class BatchRejectEntitiesRequest(BaseModel):
    reviewer: str
    note: str = ""
    source_doc_uuid: str = ""
    entity_type: str = ""
    source_site: str = ""


class BatchApproveRelationsRequest(BaseModel):
    reviewer: str
    note: str = ""
    source_doc_uuid: str = ""
    source_site: str = ""
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class BatchRejectRelationsRequest(BaseModel):
    reviewer: str
    note: str = ""
    source_doc_uuid: str = ""
    source_site: str = ""


class WeknoraBatchRequest(BaseModel):
    limit: int = Field(default=100, ge=1, description="单次同步文档数量上限")


class RecrawlRequest(BaseModel):
    """重爬请求体（目前无额外参数，预留扩展）"""
    pass


class BatchRecrawlRequest(BaseModel):
    """批量重爬请求体"""
    uuids: list[str] = Field(..., min_length=1, description="需要重爬的文档 UUID 列表")


class GraphSyncRequest(BaseModel):
    limit: int = Field(default=500, ge=1, le=500, description="单次最多处理 500 条")


class FullGraphSyncRequest(BaseModel):
    kb_id: str = Field(default="", description="目标知识库 ID，留空则使用 .env 配置")


class ResolveMergeRequest(BaseModel):
    reviewer: str
    resolution: str  # accept_new / keep_existing / custom
    resolved_value: Optional[dict] = None  # custom 时必填


class PatchEntityRequest(BaseModel):
    entity_data: dict = Field(default_factory=dict, description="需要合并的实体元数据字段")


class PatchRelationRequest(BaseModel):
    relation_props: dict = Field(default_factory=dict, description="需要合并的关系属性字段")


class MergeEntitiesRequest(BaseModel):
    keeper_dict_id: int
    absorbed_dict_ids: list[int]
    field_decisions: dict = Field(default_factory=dict)
    reviewer: str = ""
    note: str = ""


class BatchResolveMergeRequest(BaseModel):
    decisions: list[dict] = Field(default_factory=list)
    reviewer: str = ""


class RollbackMergeRequest(BaseModel):
    merge_log_id: int
    reason: str = ""


class IdentityProposalDecisionRequest(BaseModel):
    reviewer: str
    note: str = ""


class PrimeKGMappingRequest(BaseModel):
    dict_id: int
    namespace: str = Field(min_length=1)
    native_id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: dict = Field(default_factory=dict)
