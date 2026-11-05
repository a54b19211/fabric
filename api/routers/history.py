from typing import Literal

"""有独立许可的历史资料接口；旧documents/research接口行为不变。"""

import hmac
import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
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
from biz_rules.reviewed_graph import GraphNeighborhood, GraphResolution, PrimeKGLookup, PrimeKGQuery
from flows.graph_query import (
    PrimeKGResult, PrimeKGLookupResult, lookup_primekg, query_primekg,
)
from biz_rules.graph_tools import GraphPaths, GraphSubgraph, ReviewScope
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
    "/graph/primekg/lookup", response_model=PrimeKGLookupResult,
    dependencies=[Depends(require_graph_query)],
)
def primekg_lookup(body: PrimeKGLookup):
    return _call(lookup_primekg, body)


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


@router.post("/local-files", response_model=RunView, dependencies=[Depends(require_admin)])
def local_file(
    file: UploadFile = File(...),
    collection: str = Form(min_length=1, max_length=120),
    is_demo: bool = Form(False),
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    """本地文件原件导入（multipart：file、collection、is_demo）。原件按 sha256 落盘 → 一条附件版本
    （来源 local_files）→ 加工层附件解析。类型白名单与大小上限见 collection.local_files。"""
    from collection.local_files import import_upload, max_upload_bytes

    limit = max_upload_bytes()
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "文件超过上传上限"})
    return _call(import_upload, content, file.filename or "", collection, is_demo, idempotency_key)


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


@router.get("/registry-changes", dependencies=[Depends(require_read)])
def registry_changes(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
):
    """一页版本各自与上一版的登记字段差分（加工层派生，处理器版本固定）。"""
    from db.registry_changes import for_versions

    return _call(for_versions, source_id, is_demo, version_ids, mode, cutoff)


@router.get("/derived-text", dependencies=[Depends(require_read)])
def derived_text(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
):
    """一页版本的加工层派生正文（附件解析结果，含失败原因）。"""
    from db.derived_text import for_versions

    return _call(for_versions, source_id, is_demo, version_ids, mode, cutoff)


@router.get("/clean-text", dependencies=[Depends(require_read)])
def clean_text_route(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
    processor_version: str | None = None,
):
    """一页版本的清洗正文（带定位）、编号和画像（分类、四维质量、分流）。v3 3.3。
    processor_version：只取这个处理器版本的产物（v3 6.1 按引用回读）；不给就是当前处理器。"""
    from db.clean_stage import for_versions

    return _call(for_versions, source_id, is_demo, version_ids, mode, cutoff, processor_version)


@router.get("/extraction-checks", dependencies=[Depends(require_read)])
def extraction_checks_route(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
):
    """v3 6.2: the extraction-consistency checks of visible versions (BioCheck), per producer. A version without a
    completed check is "unchecked"; consumers use only completed, explicit results and never raise a state."""
    from db.extraction_checks import for_versions

    return _call(for_versions, source_id, is_demo, version_ids, mode, cutoff)


class SearchRequest(BaseModel):
    """检索请求（v3 3.6）。cutoff 不给就是现在；mode 决定按哪种时间判可见（见 db.retrieval）。"""
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    mode: Mode = "current"
    cutoff: datetime | None = None
    sources: list[str] = Field(default_factory=list, max_length=16)
    scopes: list[str] = Field(default_factory=list, max_length=16)
    is_demo: bool | None = None
    doc_categories: list[str] = Field(default_factory=list, max_length=16)
    evidence_types: list[str] = Field(default_factory=list, max_length=16)
    top_k: int = Field(default=10, ge=1, le=20)


@router.post("/search", dependencies=[Depends(require_read)])
def search_route(body: SearchRequest):
    """检索的唯一入口：WeKnora 召回候选，Fabric 逐条校验（来源许可、本模式和截止时间下可见的版本、固定正文），
    只有通过的片段才重排、才返回。剔除的给原因计数；不足 top_k 如实写明。"""
    from datetime import timezone

    from db import retrieval

    cutoff = body.cutoff or datetime.now(timezone.utc)
    return _call(retrieval.search, body.query, body.mode, cutoff, sources=body.sources, scopes=body.scopes,
                 is_demo=body.is_demo, doc_categories=body.doc_categories, evidence_types=body.evidence_types,
                 top_k=body.top_k)


@router.get("/collection/catalog", dependencies=[Depends(require_read)])
def collection_catalog():
    """来源目录：已接通、已登记未接通、被拒绝（含原因）的来源，缺口如实列出。"""
    from dataclasses import asdict

    from collection.catalog import SPECS, gaps

    return {
        "schema_version": "datafabric.collection.catalog.v1",
        "sources": [asdict(s) | {"fetchable": s.fetchable, "watchable": s.watchable} for s in SPECS],
        "gaps": gaps(),
    }


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
    review_scope: ReviewScope | None = None,
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
        review_scope=review_scope,
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
    review_scope: ReviewScope | None = None,
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
        review_scope=review_scope,
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


@router.get("/graph/{version_id}/paths", response_model=GraphPaths,
            dependencies=[Depends(require_read)])
def graph_paths(version_id: str, source_id: str, source_record_id: str, is_demo: bool,
                mode: Mode, cutoff: datetime, start: str, target: str,
                relation_types: Annotated[list[str] | None, Query()] = None,
                direction: Literal["out", "in", "both"] = "out",
                review_scope: ReviewScope = "reviewed",
                max_depth: int = Query(default=4, ge=1, le=4),
                max_paths: int = Query(default=10, ge=1, le=20),
                max_expansions: int = Query(default=1000, ge=1, le=5000)):
    return _call(store.read_graph_paths, version_id, source_id, source_record_id,
                 is_demo, mode, cutoff, start=start, target=target,
                 relation_types=relation_types or (), direction=direction,
                 review_scope=review_scope, max_depth=max_depth, max_paths=max_paths,
                 max_expansions=max_expansions)


@router.get("/graph/{version_id}/subgraph", response_model=GraphSubgraph,
            dependencies=[Depends(require_read)])
def graph_subgraph(version_id: str, source_id: str, source_record_id: str, is_demo: bool,
                   mode: Mode, cutoff: datetime,
                   node_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
                   relation_types: Annotated[list[str] | None, Query()] = None,
                   review_scope: ReviewScope = "reviewed",
                   max_nodes: int = Query(default=100, ge=1, le=100),
                   max_edges: int = Query(default=200, ge=1, le=200)):
    return _call(store.read_graph_subgraph, version_id, source_id, source_record_id,
                 is_demo, mode, cutoff, node_ids=node_ids,
                 relation_types=relation_types or (), review_scope=review_scope,
                 max_nodes=max_nodes, max_edges=max_edges)


class NativeGraphVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_record_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=64, max_length=64)


class NativeGraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[NativeGraphVersion] = Field(min_length=1, max_length=20)
    is_demo: bool
    mode: Mode
    cutoff: datetime
    max_nodes: int = Field(default=100, ge=1, le=100)
    max_edges: int = Field(default=200, ge=1, le=200)


@router.post("/graph/native/capture", response_model=RunView,
             dependencies=[Depends(require_admin)])
def native_graph(body: NativeGraphRequest,
                 idempotency_key: str = Header(alias="Idempotency-Key")):
    from flows.graph_publish import publish_native_graph

    records = [_call(store.read_version, item.version_id, "clinicaltrials",
                     item.source_record_id, body.is_demo, body.mode, body.cutoff)
               for item in body.records]
    return _call(publish_native_graph, records, idempotency_key,
                 max_nodes=body.max_nodes, max_edges=body.max_edges)


@router.get("/index/status", dependencies=[Depends(require_admin)])
def index_status(
    limit: int = Query(default=100, ge=1, le=100),
    state: str | None = Query(default=None, max_length=16),
    source_id: str | None = Query(default=None, min_length=1, max_length=80),
    cursor: str | None = Query(default=None, max_length=128),
):
    """正文进 WeKnora 的唯一路径（index_publications）的作业列表；按状态/来源过滤，next_cursor 续页。"""
    from db.index_publication import status

    return _call(status, limit=limit, state=state, source_id=source_id, cursor=cursor)


@router.get("/index/counts", dependencies=[Depends(require_admin)])
def index_counts():
    """各状态作业数，未出现的状态记0。"""
    from db.index_publication import STATES, state_counts

    counts = _call(state_counts)
    return {"states": {s: counts.get(s, 0) for s in STATES} | counts, "total": sum(counts.values())}


@router.get("/index/scheduler", dependencies=[Depends(require_admin)])
def index_scheduler():
    """T-30：按作业类别 x 来源的到期、在租、最久等待和最近领取；按知识库的在途预算、发送令牌、闸门拒绝与阻塞时长；
    最近一小时首发、ready、失败数。"""
    from db.index_publication import scheduler_status

    return _call(scheduler_status)


@router.post("/index/{publication_id}/retry", dependencies=[Depends(require_admin)])
def index_retry(publication_id: str):
    from db.index_publication import retry

    return _call(retry, publication_id)
