"""请求体 Pydantic 模型"""
from typing import Optional
from pydantic import BaseModel


class ExtractionTriggerRequest(BaseModel):
    uuids: list[str] = []
    limit: int = 10


class ReviewActionRequest(BaseModel):
    reviewer: str
    note: str = ""


class Neo4jSyncRequest(BaseModel):
    limit: int = 100


class Neo4jRetryRequest(BaseModel):
    object_type: str   # "entity" or "relation"
    object_id: int


class WeknoraBatchRequest(BaseModel):
    limit: int = 100
