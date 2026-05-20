"""请求体 Pydantic 模型"""
from typing import Optional
from pydantic import BaseModel


class ExtractionTriggerRequest(BaseModel):
    uuids: list[str] = []
    limit: int = 10


class ReviewActionRequest(BaseModel):
    reviewer: str
    note: str = ""


class BatchApproveEntitiesRequest(BaseModel):
    reviewer: str
    note: str = ""
    source_doc_uuid: str = ""
    entity_type: str = ""
    source_site: str = ""
    min_confidence: float = 0.0


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
    min_confidence: float = 0.0


class BatchRejectRelationsRequest(BaseModel):
    reviewer: str
    note: str = ""
    source_doc_uuid: str = ""
    source_site: str = ""


class Neo4jSyncRequest(BaseModel):
    limit: int = 100


class Neo4jRetryRequest(BaseModel):
    object_type: str   # "entity" or "relation"
    object_id: int


class WeknoraBatchRequest(BaseModel):
    limit: int = 100
