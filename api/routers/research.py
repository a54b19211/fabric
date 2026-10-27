"""Bounded, read-only access to existing ClinicalTrials-marked records."""

import hashlib
from datetime import datetime, timezone
from typing import List, Literal, Optional
from uuid import UUID

from biz_rules.clinical_research import (
    MAX_DOCUMENT_BYTES,
    ClinicalTrialProjection,
    project_clinical_trial,
)
from db.models import Document
from db.session import get_session
from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import case, func, text
from sqlalchemy.exc import SQLAlchemyError

router = APIRouter(prefix="/api/research/documents", tags=["research"])
SEARCH_LIMIT = 20


class ResearchDocumentItem(BaseModel):
    uuid: str
    title: str
    source_site: Literal["clinicaltrials"] = "clinicaltrials"
    updated_at: Optional[str] = None


class ResearchSearchResponse(BaseModel):
    schema_version: Literal["datafabric.research.search.v1"] = "datafabric.research.search.v1"
    source_site: Literal["clinicaltrials"] = "clinicaltrials"
    scope: Literal["title_literal_and_current_db"] = "title_literal_and_current_db"
    terms: list[str]
    limit: int
    has_more: bool
    observed_at: str
    items: list[ResearchDocumentItem]


class ResearchDocumentResponse(BaseModel):
    schema_version: Literal["datafabric.research.document.v1"] = "datafabric.research.document.v1"
    uuid: str
    title: str
    source_site: Literal["clinicaltrials"] = "clinicaltrials"
    source_url: Optional[str] = None
    publish_date_raw: Optional[str] = None
    crawl_timestamp: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    observed_at: str
    document_sha256: str
    projection: ClinicalTrialProjection


def _configure_read(session):
    session.execute(text("SET TRANSACTION READ ONLY"))
    session.execute(text("SET LOCAL statement_timeout = '3000ms'"))


def _unavailable(error):
    original = getattr(error, "orig", None)
    timeout = (
        isinstance(error, SQLAlchemyError)
        and (getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)) == "57014"
    )
    logger.warning("[Research] 只读资料查询超时或不可用")
    return HTTPException(
        status_code=503,
        detail={
            "code": "research_query_timeout" if timeout else "research_unavailable",
            "message": "资料查询超时或不可用，未返回成功结果；请缩小范围后核对。",
        },
    )


def _iso(value):
    return value.isoformat() if value is not None else None


@router.get(
    "/search", response_model=ResearchSearchResponse, responses={503: {"description": "查询失败或超时"}}
)
def search_documents(
    terms: List[str] = Query(
        ..., min_length=1, max_length=5, description="1至5个标题字面词组，全部匹配；不支持语义或正文检索"
    ),
    limit: int = Query(6, ge=1, le=SEARCH_LIMIT),
):
    terms = [term.strip() for term in terms]
    if any(not term or len(term) > 128 for term in terms):
        raise HTTPException(status_code=422, detail="每个标题词组须为1至128字符")
    try:
        with get_session() as session:
            if session is None:
                raise HTTPException(
                    status_code=503, detail={"code": "research_unavailable", "message": "数据库未连接"}
                )
            _configure_read(session)
            query = session.query(
                Document.uuid, Document.title, Document.source_site, Document.updated_at
            ).filter(
                Document.is_deleted.is_(False),
                Document.source_site == "clinicaltrials",
            )
            for term in terms:
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                query = query.filter(Document.title.ilike("%" + escaped + "%", escape="\\"))
            rows = query.order_by(Document.id.asc()).limit(limit + 1).all()
        return ResearchSearchResponse(
            terms=terms,
            limit=limit,
            has_more=len(rows) > limit,
            observed_at=datetime.now(timezone.utc).isoformat(),
            items=[
                ResearchDocumentItem(uuid=row.uuid, title=row.title or "", updated_at=_iso(row.updated_at))
                for row in rows[:limit]
            ],
        )
    except HTTPException:
        raise
    except Exception as error:
        raise _unavailable(error) from None


@router.get(
    "/{uuid}",
    response_model=ResearchDocumentResponse,
    responses={
        404: {"description": "没有限定来源的存量记录"},
        413: {"description": "文档超出读取预算"},
        503: {"description": "读取失败或超时"},
    },
)
def read_document(uuid: UUID):
    try:
        with get_session() as session:
            if session is None:
                raise HTTPException(
                    status_code=503, detail={"code": "research_unavailable", "message": "数据库未连接"}
                )
            _configure_read(session)
            size = func.octet_length(Document.raw_content)
            # Do not transfer an oversized historical body into the API process.
            raw = case((size <= MAX_DOCUMENT_BYTES, Document.raw_content), else_=None)
            row = (
                session.query(
                    Document.uuid,
                    Document.title,
                    Document.source_site,
                    Document.source_url,
                    Document.publish_date,
                    Document.crawl_timestamp,
                    Document.created_at,
                    Document.updated_at,
                    size.label("raw_bytes"),
                    raw.label("raw_content"),
                )
                .filter(
                    Document.uuid == str(uuid),
                    Document.is_deleted.is_(False),
                    Document.source_site == "clinicaltrials",
                )
                .first()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="未找到限定来源中的记录")
            if row.raw_bytes is not None and row.raw_bytes > MAX_DOCUMENT_BYTES:
                raise HTTPException(status_code=413, detail="存量文档超过2MiB读取预算，未投影")
            raw_content = row.raw_content or ""
        return ResearchDocumentResponse(
            uuid=row.uuid,
            title=row.title or "",
            source_url=row.source_url,
            publish_date_raw=row.publish_date,
            crawl_timestamp=_iso(row.crawl_timestamp),
            created_at=_iso(row.created_at),
            updated_at=_iso(row.updated_at),
            observed_at=datetime.now(timezone.utc).isoformat(),
            document_sha256=hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
            projection=project_clinical_trial(raw_content),
        )
    except HTTPException:
        raise
    except Exception as error:
        raise _unavailable(error) from None
