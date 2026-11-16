"""雷达读的资料库接口（/api/fabric/*）：来源、版本、变更、清洗正文、附件正文、登记差分、上一版、检索、
采集任务。读接口要读凭据（DATAFABRIC_READ_TOKEN 或管理员凭据），写接口要管理员凭据。"""

import hmac
import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from store import records as store
from store.record_models import (
    ArchiveImport,
    FabricError,
    FetchRequest,
    Mode,
    RecordCollection,
    RecordKind,
    RunView,
    VersionRecord,
)

router = APIRouter(prefix="/api/fabric", tags=["资料库"])
bearer = HTTPBearer(auto_error=False, scheme_name="FabricBearer")


def _credential(credentials):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={
                "code": "authentication_required",
                "message": "需要本地资料库服务凭据",
            },
        )
    return credentials.credentials


def require_read(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    token = _credential(credentials)
    expected = [
        os.environ.get("DATAFABRIC_READ_TOKEN", ""),
        os.environ.get("DATAFABRIC_ADMIN_TOKEN", ""),
    ]
    if not any(expected):
        raise HTTPException(
            status_code=503,
            detail={"code": "fabric_not_configured", "message": "资料库服务尚未配置"},
        )
    if not any(value and hmac.compare_digest(token, value) for value in expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_credential", "message": "凭据无效"},
        )
    return "fabric-reader"


def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    token = _credential(credentials)
    expected = os.environ.get("DATAFABRIC_ADMIN_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=403,
            detail={"code": "admin_required", "message": "需要资料库管理员凭据"},
        )
    return "fabric-admin"


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except FabricError as exc:
        raise HTTPException(
            status_code=exc.status, detail={"code": exc.code, "message": exc.message}
        ) from None
    except Exception:  # noqa: BLE001 -- HTTP错误边界不得回显连接串或任意异常
        # 不把DB连接串、上游响应/URL或其他任意异常文本传给调用者。
        raise HTTPException(
            status_code=503,
            detail={
                "code": "fabric_service_failed",
                "message": "资料库服务暂不可用；未确认写入请先按任务ID或幂等键核对",
            },
        ) from None


def _read_version(version_id, source_id, source_record_id, is_demo, mode, cutoff):
    """一个固定版本（旧的图版本照常读出原件，不再核对引文）。"""
    return store.read_version(version_id, source_id, source_record_id, is_demo, mode, cutoff)


class SourceList(BaseModel):
    capabilities_version: str
    sources: list[dict]
    retired_sources: list[dict]


@router.get("/sources", response_model=SourceList, dependencies=[Depends(require_read)])
def sources():
    return _call(store.sources_receipt)


@router.post("/archives", response_model=RunView, dependencies=[Depends(require_admin)])
def archive(body: ArchiveImport, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """导入一包原件。图包来源（reviewed_graph、primekg_graph）不再能导入。"""
    return _call(store.import_archive, body, idempotency_key)


@router.post("/local-files", response_model=RunView, dependencies=[Depends(require_admin)])
def local_file(
    file: UploadFile = File(...),
    collection: str = Form(min_length=1, max_length=120),
    is_demo: bool = Form(False),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """本地文件原件导入（multipart：file、collection、is_demo）。原件按 sha256 落盘 → 一条附件版本
    （来源 local_files）→ 加工层附件解析。类型白名单与大小上限见 fetch.local_files。"""
    from fetch.local_files import import_upload, max_upload_bytes

    limit = max_upload_bytes()
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "文件超过上传上限"})
    return _call(import_upload, content, file.filename or "", collection, is_demo, idempotency_key)


@router.post("/fetch", response_model=RunView, dependencies=[Depends(require_admin)])
def fetch(body: FetchRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """建一条采集任务并放进队列（幂等键可选：同一个键再提交，返回原来那条任务）。"""
    from fetch.fetch_job import enqueue_fetch
    from fetch.sources import require_public_fetch_enabled

    _call(require_public_fetch_enabled)
    run, fresh = _call(store.create_fetch, body, idempotency_key)
    return _call(enqueue_fetch, run) if fresh else run


@router.get(
    "/runs/{run_id}", response_model=RunView, dependencies=[Depends(require_read)]
)
def run(run_id: str):
    return _call(store.read_run, run_id)


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
    window_start: datetime | None = None,
    window_end: datetime | None = None,
):
    """window_start / window_end (both, with a time zone; GDELT only) list the leads observed in [start, end) and
    put the receipts of that window into coverage.window_coverage and coverage.truncated_intervals."""
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
        window=(window_start, window_end),
    )


@router.get("/versions", dependencies=[Depends(require_read)])
def versions(version_ids: str = Query(min_length=64, max_length=6500)):
    """按版本号批量取版本（逗号分隔，最多 100 个）：条目和 /changes 的一样，不存在或不可读的在 missing 里。"""
    return _call(store.versions_by_id, [v for v in version_ids.split(",") if v])


@router.get(
    "/versions/{version_id}",
    response_model=VersionRecord,
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
    return _call(_read_version, version_id, source_id, source_record_id, is_demo, mode, cutoff)


@router.get("/versions/{version_id}/raw", dependencies=[Depends(require_read)])
def raw(
    version_id: str,
    source_id: str,
    source_record_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
):
    record = _call(_read_version, version_id, source_id, source_record_id, is_demo, mode, cutoff)
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
    from clean.registry_changes import for_versions

    return _call(for_versions, source_id, is_demo, version_ids, mode, cutoff)


@router.get("/version-differences", dependencies=[Depends(require_read)])
def version_differences(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
):
    """一页版本各自的上一版（store.records.version_differences）。"""
    return _call(store.version_differences, source_id, is_demo, version_ids, mode, cutoff)


@router.get("/derived-text", dependencies=[Depends(require_read)])
def derived_text(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
):
    """一页版本的加工层派生正文（附件解析结果，含失败原因）。"""
    from clean.derived_text import for_versions

    return _call(for_versions, source_id, is_demo, version_ids, mode, cutoff)


@router.get("/clean-text", dependencies=[Depends(require_read)])
def clean_text_route(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
    processor_version: str | None = None,
    quality: bool = False,
):
    """一页版本的清洗正文（带定位）、编号和画像（分类、四维质量、分流）。v3 3.3。
    processor_version：只取这个处理器版本的产物（v3 6.1 按引用回读）；不给就是当前处理器。
    quality=true：看得到的版本另带 quality = {level: 高/中/低/隔离, basis: "profile"}，由画像折算
    （clean/profile.py · quality_level；没画像是 null）。
    不加这个参数时返回和以前一样（雷达不加）。"""
    from clean.clean_stage import for_versions

    result = _call(for_versions, source_id, is_demo, version_ids, mode, cutoff, processor_version)
    if quality:
        from clean.profile import quality_level

        for entry in result["versions"].values():
            if entry["state"] != "not_visible_or_not_processed":
                level = quality_level(entry.get("profile"))
                entry["quality"] = {"level": level, "basis": "profile"} if level else None
    return result


class SearchRequest(BaseModel):
    """检索请求（v3 3.6）。cutoff 不给就是现在；mode 决定按哪种时间判可见（见 store.retrieval）。"""
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

    from store import retrieval

    cutoff = body.cutoff or datetime.now(timezone.utc)
    return _call(retrieval.search, body.query, body.mode, cutoff, sources=body.sources, scopes=body.scopes,
                 is_demo=body.is_demo, doc_categories=body.doc_categories, evidence_types=body.evidence_types,
                 top_k=body.top_k)


@router.get("/collection/catalog", dependencies=[Depends(require_read)])
def collection_catalog():
    """来源目录：已接通、已登记未接通、被拒绝（含原因）的来源，缺口如实列出。"""
    from dataclasses import asdict

    from fetch.catalog import SPECS, gaps

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
):
    return _call(store.list_changes, source_id, is_demo, after=after, limit=limit)


@router.get("/source-health", dependencies=[Depends(require_read)])
def source_health(source_id: str = Query(min_length=1, max_length=80)):
    from fetch.source_refresh import source_health as health

    return _call(health, source_id)


@router.get("/index/status", dependencies=[Depends(require_admin)])
def index_status(
    limit: int = Query(default=100, ge=1, le=100),
    state: str | None = Query(default=None, max_length=16),
    source_id: str | None = Query(default=None, min_length=1, max_length=80),
    cursor: str | None = Query(default=None, max_length=128),
):
    """正文进 WeKnora 的唯一路径（index_publications）的作业列表；按状态/来源过滤，next_cursor 续页。"""
    from store.index_publication import status

    return _call(status, limit=limit, state=state, source_id=source_id, cursor=cursor)


@router.get("/index/counts", dependencies=[Depends(require_admin)])
def index_counts():
    """各状态作业数，未出现的状态记0。"""
    from store.index_publication import STATES, state_counts

    counts = _call(state_counts)
    return {"states": {s: counts.get(s, 0) for s in STATES} | counts, "total": sum(counts.values())}


@router.post("/index/{publication_id}/retry", dependencies=[Depends(require_admin)])
def index_retry(publication_id: str):
    from store.index_publication import retry

    return _call(retry, publication_id)


class SourceSwitches(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read_enabled: bool | None = None
    fetch_enabled: bool | None = None
    changed_by: str = Field(default="fabric-admin", min_length=1, max_length=80)  # 谁改的，记进 fabric_source_switch_log


@router.get("/admin/sources", dependencies=[Depends(require_admin)])
def admin_sources():
    """每个来源的 read_enabled、fetch_enabled、最近一次采集任务、各固定刷新的状态、最近一次改开关（fetch/source_admin.py）。"""
    from fetch.source_admin import list_sources

    return _call(list_sources)


@router.patch("/admin/sources/{source_id}", dependencies=[Depends(require_admin)])
def admin_source_switches(source_id: str, body: SourceSwitches):
    from fetch.source_admin import set_switches

    return _call(set_switches, source_id, body.changed_by, body.read_enabled, body.fetch_enabled)
