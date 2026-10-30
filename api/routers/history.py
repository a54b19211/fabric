from typing import Literal

"""有独立许可的历史资料接口；旧documents/research接口行为不变。"""

import hmac
import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from biz_rules.history_models import (
    BUSINESS_TYPES,
    ArchiveImport,
    FetchRequest,
    HistoryError,
    HistoryRecord,
    Mode,
    RecordCollection,
    RecordKind,
    RunView,
)
from biz_rules.reviewed_graph import GraphNeighborhood, GraphResolution, PrimeKGQuery
from flows.graph_query import PrimeKGResult, query_primekg
from db import history as store


class BoundedHistoryRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def bounded(request: Request):
            if request.method == "POST":
                size, chunks = 0, []
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise HTTPException(
                            status_code=413,
                            detail={
                                "code": "request_budget",
                                "message": "请求超过16MiB预算",
                            },
                        )
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            return await original(request)

        return bounded


router = APIRouter(
    prefix="/api/history", tags=["历史资料"], route_class=BoundedHistoryRoute
)
bearer = HTTPBearer(auto_error=False, scheme_name="HistoryBearer")


def _credential(credentials):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={
                "code": "authentication_required",
                "message": "需要本地历史服务凭据",
            },
        )
    return credentials.credentials


def require_read(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    token = _credential(credentials)
    expected = [
        os.environ.get("DATAFABRIC_HISTORY_READ_TOKEN", ""),
        os.environ.get("DATAFABRIC_HISTORY_ADMIN_TOKEN", ""),
    ]
    if not any(expected):
        raise HTTPException(
            status_code=503,
            detail={"code": "history_not_configured", "message": "历史服务尚未配置"},
        )
    if not any(value and hmac.compare_digest(token, value) for value in expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_credential", "message": "凭据无效"},
        )
    return "history-reader"


def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    token = _credential(credentials)
    expected = os.environ.get("DATAFABRIC_HISTORY_ADMIN_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=403,
            detail={"code": "admin_required", "message": "需要历史资料管理员凭据"},
        )
    return "history-admin"


def require_graph_query(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    token = _credential(credentials)
    expected = os.environ.get("DATAFABRIC_GRAPH_QUERY_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail={
            "code": "graph_query_not_configured", "message": "当前图查询尚未配置"
        })
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail={
            "code": "graph_query_credential_required", "message": "需要当前图查询凭据"
        })


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except HistoryError as exc:
        raise HTTPException(
            status_code=exc.status, detail={"code": exc.code, "message": exc.message}
        ) from None
    except Exception:  # noqa: BLE001 -- HTTP错误边界不得回显连接串或任意异常
        # 不把DB连接串、上游响应/URL或其他任意异常文本传给调用者。
        raise HTTPException(
            status_code=503,
            detail={
                "code": "history_service_failed",
                "message": "历史资料服务暂不可用；未确认写入请先按任务ID或幂等键核对",
            },
        ) from None


class SourceList(BaseModel):
    business_types: list[str]
    sources: list[dict]


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_attempt: int = Field(ge=1)


@router.get("/sources", response_model=SourceList, dependencies=[Depends(require_read)])
def sources():
    return {
        "business_types": list(BUSINESS_TYPES),
        "sources": _call(store.list_sources),
    }


@router.post(
    "/graph/primekg/capture", response_model=PrimeKGResult,
    dependencies=[Depends(require_graph_query)],
)
def primekg_capture(
    body: PrimeKGQuery, idempotency_key: str = Header(alias="Idempotency-Key")
):
    return _call(query_primekg, body, idempotency_key)


@router.post("/archives", response_model=RunView, dependencies=[Depends(require_admin)])
def archive(
    body: ArchiveImport, idempotency_key: str = Header(alias="Idempotency-Key")
):
    return _call(store.import_archive, body, idempotency_key)


@router.post("/fetch", response_model=RunView, dependencies=[Depends(require_admin)])
def fetch(body: FetchRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
    from flows.history_fetch import enqueue_fetch

    run, fresh = _call(store.create_fetch, body, idempotency_key)
    return _call(enqueue_fetch, run) if fresh else run


@router.get(
    "/runs/{run_id}", response_model=RunView, dependencies=[Depends(require_read)]
)
def run(run_id: str):
    return _call(store.read_run, run_id)


@router.post(
    "/runs/{run_id}/retry",
    response_model=RunView,
    dependencies=[Depends(require_admin)],
)
def retry(run_id: str, body: RetryRequest):
    from flows.history_fetch import retry_inactive

    return _call(retry_inactive, run_id, body.expected_attempt)


@router.get(
    "/records", response_model=RecordCollection, dependencies=[Depends(require_read)]
)
def records(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    run_id: str | None = None,
    source_record_ids: Annotated[list[str] | None, Query()] = None,
    terms: Annotated[list[str] | None, Query()] = None,
    record_kind: RecordKind | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
):
    return _call(
        store.list_records,
        source_id,
        is_demo,
        mode,
        cutoff,
        run_id=run_id,
        source_record_ids=source_record_ids or (),
        terms=terms or (),
        record_kind=record_kind,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/versions/{version_id}",
    response_model=HistoryRecord,
    dependencies=[Depends(require_read)],
)
def version(
    version_id: str,
    source_id: str,
    source_record_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
):
    return _call(
        store.read_version,
        version_id,
        source_id,
        source_record_id,
        is_demo,
        mode,
        cutoff,
    )


@router.get("/versions/{version_id}/raw", dependencies=[Depends(require_read)])
def raw(
    version_id: str,
    source_id: str,
    source_record_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
):
    record = _call(
        store.read_version,
        version_id,
        source_id,
        source_record_id,
        is_demo,
        mode,
        cutoff,
    )
    return Response(
        content=record.raw_content,
        media_type=record.raw_media_type,
        headers={"X-Content-SHA256": record.raw_sha256},
    )


@router.get("/changes", dependencies=[Depends(require_read)])
def changes(
    source_id: str,
    is_demo: bool,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
):
    return _call(
        store.list_changes, source_id, is_demo, after=after, limit=limit, cursor=cursor
    )


@router.get(
    "/graph/{version_id}/neighborhood",
    response_model=GraphNeighborhood,
    dependencies=[Depends(require_read)],
)
def graph_neighborhood(
    version_id: str,
    source_id: str,
    source_record_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    seed_ids: Annotated[list[str], Query()],
    relation_types: Annotated[list[str] | None, Query()] = None,
    direction: str = "both",
    max_depth: int = Query(default=1, ge=0, le=2),
    max_nodes: int = Query(default=50, ge=1, le=100),
    max_edges: int = Query(default=100, ge=1, le=200),
):
    return _call(
        store.read_graph_neighborhood,
        version_id,
        source_id,
        source_record_id,
        is_demo,
        mode,
        cutoff,
        seed_ids=seed_ids,
        relation_types=relation_types or (),
        direction=direction,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )


@router.get(
    "/graph/{version_id}/resolve",
    response_model=GraphResolution,
    dependencies=[Depends(require_read)],
)
def graph_resolve(
    version_id: str,
    source_id: str,
    source_record_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    name: str | None = None,
    external_namespace: str | None = None,
    external_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=20),
):
    return _call(
        store.read_graph_resolution,
        version_id,
        source_id,
        source_record_id,
        is_demo,
        mode,
        cutoff,
        name=name,
        external_namespace=external_namespace,
        external_id=external_id,
        limit=limit,
    )


@router.get("/publication/status", dependencies=[Depends(require_read)])
def publication_status(
    source_id: Literal["legacy_documents", "reviewed_graph"] = "legacy_documents",
):
    from db.publication import publication_status as read_status

    return _call(read_status, source_id)


@router.get("/source-health", dependencies=[Depends(require_read)])
def source_health(source_id: str = Query(min_length=1, max_length=80)):
    from db.source_refresh import source_health as health

    return _call(health, source_id)
