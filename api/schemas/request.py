"""请求体 Pydantic 模型"""
from typing import Optional
from pydantic import BaseModel, Field, validator


class ExtractionTriggerRequest(BaseModel):
    uuids: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=500, description="最多处理 500 篇文档")


class ReviewActionRequest(BaseModel):
    reviewer: str
    note: str = ""


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


class Neo4jSyncRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=500, description="最多处理 500 条记录")


class Neo4jRetryRequest(BaseModel):
    object_type: str   # "entity" or "relation"
    object_id: int = Field(ge=1, description="ID 必须大于 0")


class WeknoraBatchRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=500, description="最多处理 500 篇文档")


class RecrawlRequest(BaseModel):
    """重爬请求体（目前无额外参数，预留扩展）"""
    pass


class BatchRecrawlRequest(BaseModel):
    """批量重爬请求体"""
    uuids: list[str] = Field(..., min_length=1, description="需要重爬的文档 UUID 列表")


class ResolveMergeRequest(BaseModel):
    reviewer: str
    resolution: str  # accept_new / keep_existing / custom
    resolved_value: Optional[dict] = None  # custom 时必填
