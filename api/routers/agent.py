# -*- coding: utf-8 -*-
"""Agent 自动审核 API — 触发 Agent 运行 + 查询运行记录。"""

import asyncio
import json
import threading

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger

from core_engine.agent_runner import get_agent_runner
from db.agent_run_repo import (
    get_agent_run, list_agent_runs, list_runs_by_document,
    get_agent_stats, get_runs_for_document, count_pending_entities,
    list_documents_with_entities,
    mark_document_reviewing, unmark_document_reviewing,
)
from db.session import get_session
from db.models import ExtractedEntity, EntityRelation, Document
from api.schemas.response import PipelineOverviewResponse, PipelineStage, EntityReviewStatsResponse

router = APIRouter(prefix="/api/agent", tags=["agent"])


# ── Schemas ────────────────────────────────────────────────────────────────

class AgentRunResponse(BaseModel):
    run_id: str = ""
    status: str = "queued"
    message: str = ""


class AgentReviewResult(BaseModel):
    run_id: str = ""
    decision: str = ""
    reason: str = ""
    confidence: float = 0.0
    suggested_dict_id: int | None = None
    dict_id: int | None = None
    merge_queue_id: int | None = None
    entity_name: str = ""
    document_title: str = ""
    steps: list[dict] = Field(default_factory=list)


class BatchReviewRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    entity_type: str = ""
    source_site: str = ""


class BatchReviewResponse(BaseModel):
    queued: int = 0
    results: list[AgentReviewResult] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class ConflictResolutionRequest(BaseModel):
    merge_id: int
    dict_id: int
    entity_type: str
    entity_name: str
    field_name: str
    existing_value: str = ""
    new_value: str = ""
    existing_source_site: str = ""
    new_source_site: str = ""
    existing_source_doc: str = ""
    new_source_doc: str = ""


class SemanticMatchRequest(BaseModel):
    dict_id_a: int
    dict_id_b: int
    entity_a_name: str = ""
    entity_b_name: str = ""
    entity_type: str = ""


class RunListResponse(BaseModel):
    total: int = 0
    items: list[dict] = Field(default_factory=list)


class AgentStatsResponse(BaseModel):
    stats: dict = Field(default_factory=dict)


class DocumentReviewResponse(BaseModel):
    batch_id: str = ""
    document_uuid: str = ""
    document_title: str = ""
    total: int = 0
    approved: int = 0
    rejected: int = 0
    escalated: int = 0
    error: int = 0
    relation_total: int = 0
    relation_approved: int = 0
    relation_rejected: int = 0
    relation_error: int = 0
    results: list[AgentReviewResult] = Field(default_factory=list)
    relation_results: list[AgentReviewResult] = Field(default_factory=list)


class DocumentRunSummary(BaseModel):
    document_uuid: str = ""
    document_title: str = ""
    entity_count: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    escalated_count: int = 0
    error_count: int = 0
    avg_confidence: float = 0.0
    last_review_at: str = ""
    entities: list[dict] = Field(default_factory=list)


class DocumentRunListResponse(BaseModel):
    total: int = 0
    items: list[DocumentRunSummary] = Field(default_factory=list)


class DocumentPreviewResponse(BaseModel):
    document_uuid: str = ""
    document_title: str = ""
    pending_count: int = 0


class DocumentRunsResponse(BaseModel):
    document_uuid: str = ""
    document_title: str = ""
    pending_count: int = 0
    runs: list[dict] = Field(default_factory=list)


class PendingDocumentItem(BaseModel):
    document_uuid: str = ""
    document_title: str = ""
    source_site: str = ""
    doc_type: str = ""
    pending_count: int = 0
    total_entity_count: int = 0
    pending_relation_count: int = 0
    total_relation_count: int = 0
    review_status: str = "pending"  # pending / reviewing / reviewed
    created_at: str = ""


class PendingDocumentListResponse(BaseModel):
    items: list[PendingDocumentItem] = Field(default_factory=list)
    total: int = 0


# ── 待审核文档列表 ──────────────────────────────────────────────────────────

@router.get("/documents/pending", response_model=PendingDocumentListResponse)
def list_pending_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status: str = Query("", description="pending / reviewed / 空=全部"),
):
    """分页列出所有有实体的文档（按审核状态分：pending / reviewing / reviewed）。"""
    items, total = list_documents_with_entities(page=page, page_size=page_size, status=status)
    return PendingDocumentListResponse(
        items=[PendingDocumentItem(**d) for d in items],
        total=total,
    )


# ── Agent A: 实体审核 ──────────────────────────────────────────────────────

@router.post("/review/entity/{entity_id}", response_model=AgentReviewResult)
def trigger_entity_review(entity_id: int):
    """触发单个实体自动审核。同步执行，返回结果。"""
    with get_session() as session:
        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")
        if row.review_status != "pending":
            raise HTTPException(status_code=400, detail=f"实体状态为 {row.review_status}，不可审核")

    runner = get_agent_runner()
    try:
        result = runner.run_entity_review(entity_id)
        return AgentReviewResult(**result)
    except Exception as e:
        logger.error(f"[API] 实体审核失败: entity_id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/review/entity/batch", response_model=BatchReviewResponse)
def trigger_batch_entity_review(body: BatchReviewRequest):
    """批量自动审核 —— 获取 pending 实体并并发执行。"""
    runner = get_agent_runner()
    try:
        results = runner.run_batch_pending(
            limit=body.limit,
            entity_type=body.entity_type,
            source_site=body.source_site,
        )
        decisions = {}
        for r in results:
            d = r.get("decision", "error")
            decisions[d] = decisions.get(d, 0) + 1
        return BatchReviewResponse(
            queued=len(results),
            results=[AgentReviewResult(**r) for r in results],
            summary=decisions,
        )
    except Exception as e:
        logger.error(f"[API] 批量审核失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/review/entity/batch/async", response_model=AgentRunResponse)
def trigger_batch_async(body: BatchReviewRequest):
    """批量自动审核（异步）—— 立即返回，后台线程执行。"""
    runner = get_agent_runner()

    with get_session() as session:
        entities = session.query(ExtractedEntity).filter(
            ExtractedEntity.review_status == "pending",
            ExtractedEntity.is_deleted == False,
        )
        if body.entity_type:
            entities = entities.filter(ExtractedEntity.entity_type == body.entity_type)
        if body.source_site:
            entities = entities.filter(ExtractedEntity.source_site == body.source_site)
        entities = entities.limit(body.limit).all()
        affected_uuids = list({e.source_doc_uuid for e in entities if e.source_doc_uuid})

    for uid in affected_uuids:
        mark_document_reviewing(uid)

    def _bg_review():
        try:
            results = runner.run_batch_pending(
                limit=body.limit,
                entity_type=body.entity_type,
                source_site=body.source_site,
            )
            logger.info(f"[API] 后台批量审核完成: {len(results)} 条")
        except Exception as e:
            logger.error(f"[API] 后台批量审核失败: {e}")
        finally:
            for uid in affected_uuids:
                unmark_document_reviewing(uid)

    threading.Thread(target=_bg_review, daemon=True).start()
    return AgentRunResponse(status="queued", message=f"已入队 {body.limit} 篇审核任务")


@router.post("/review/document/{document_uuid}", response_model=DocumentReviewResponse)
def trigger_document_review(document_uuid: str):
    """按文档维度审核 — 审核文档下所有 pending 实体，返回聚合结果。"""
    with get_session() as session:
        doc = session.query(Document).filter(
            Document.uuid == document_uuid,
            Document.is_deleted == False,
        ).first()
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")

    runner = get_agent_runner()
    try:
        result = runner.run_document_review(document_uuid)
        return DocumentReviewResponse(
            batch_id=result["batch_id"],
            document_uuid=result["document_uuid"],
            document_title=result["document_title"],
            total=result["total"],
            approved=result["approved"],
            rejected=result["rejected"],
            escalated=result["escalated"],
            error=result["error"],
            relation_total=result.get("relation_total", 0),
            relation_approved=result.get("relation_approved", 0),
            relation_rejected=result.get("relation_rejected", 0),
            relation_error=result.get("relation_error", 0),
            results=[AgentReviewResult(**r) for r in result["results"]],
            relation_results=[AgentReviewResult(**r) for r in result.get("relation_results", [])],
        )
    except Exception as e:
        logger.error(f"[API] 文档审核失败: document_uuid={document_uuid} | {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/review/document/{document_uuid}/async", response_model=AgentRunResponse)
def trigger_document_review_async(document_uuid: str):
    """异步按文档维度审核。"""
    with get_session() as session:
        doc = session.query(Document).filter(
            Document.uuid == document_uuid,
            Document.is_deleted == False,
        ).first()
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")

    mark_document_reviewing(document_uuid)

    def _bg_review():
        try:
            runner = get_agent_runner()
            result = runner.run_document_review(document_uuid)
            logger.info(
                f"[API] 后台文档审核完成: {document_uuid}, "
                f"实体 {result['total']} 条, 关系 {result.get('relation_total', 0)} 条"
            )
        except Exception as e:
            logger.error(f"[API] 后台文档审核失败: {document_uuid} | {e}")
        finally:
            unmark_document_reviewing(document_uuid)

    threading.Thread(target=_bg_review, daemon=True).start()
    return AgentRunResponse(
        status="queued",
        message=f"文档 {document_uuid} 已入队审核",
    )


# ── 文档审核预览 / 轮询 ───────────────────────────────────────────────────

@router.get("/review/document/{document_uuid}/preview", response_model=DocumentPreviewResponse)
def get_document_preview(document_uuid: str):
    """获取文档审核前的预览信息。"""
    with get_session() as session:
        doc = session.query(Document).filter(
            Document.uuid == document_uuid,
            Document.is_deleted == False,
        ).first()
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")

    pending = count_pending_entities(document_uuid)
    return DocumentPreviewResponse(
        document_uuid=document_uuid,
        document_title=doc.title or "",
        pending_count=pending,
    )


@router.get("/review/document/{document_uuid}/runs", response_model=DocumentRunsResponse)
def get_document_runs(document_uuid: str):
    """获取文档的实体审核运行记录（含完整步骤，用于轮询展示审核进度）。"""
    with get_session() as session:
        doc = session.query(Document).filter(
            Document.uuid == document_uuid,
            Document.is_deleted == False,
        ).first()

    pending = count_pending_entities(document_uuid)
    runs = get_runs_for_document(document_uuid)

    return DocumentRunsResponse(
        document_uuid=document_uuid,
        document_title=doc.title if doc else "",
        pending_count=pending,
        runs=runs,
    )


# ── Agent A: 关系审核 ──────────────────────────────────────────────────────

@router.post("/review/relation/{relation_id}", response_model=AgentReviewResult)
def trigger_relation_review(relation_id: int):
    """触发单个关系自动审核。"""
    with get_session() as session:
        row = session.query(EntityRelation).filter(
            EntityRelation.id == relation_id,
            EntityRelation.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="关系不存在")
        if row.review_status != "pending":
            raise HTTPException(status_code=400, detail=f"关系状态为 {row.review_status}")

    runner = get_agent_runner()
    try:
        result = runner.run_relation_review(relation_id)
        return AgentReviewResult(**result)
    except Exception as e:
        logger.error(f"[API] 关系审核失败: relation_id={relation_id} | {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Agent B: 冲突裁决 ──────────────────────────────────────────────────────

@router.post("/conflict/resolve", response_model=AgentReviewResult)
def trigger_conflict_resolution(body: ConflictResolutionRequest):
    """触发属性冲突自动裁决。"""
    runner = get_agent_runner()
    try:
        result = runner.run_conflict_resolution(
            merge_id=body.merge_id,
            dict_id=body.dict_id,
            entity_type=body.entity_type,
            entity_name=body.entity_name,
            field_name=body.field_name,
            existing_value=body.existing_value,
            new_value=body.new_value,
            existing_source_site=body.existing_source_site,
            new_source_site=body.new_source_site,
            existing_source_doc=body.existing_source_doc,
            new_source_doc=body.new_source_doc,
        )
        return AgentReviewResult(**result)
    except Exception as e:
        logger.error(f"[API] 冲突裁决失败: merge_id={body.merge_id} | {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Agent C: 语义匹配 ──────────────────────────────────────────────────────

@router.post("/match/entity-pair", response_model=AgentReviewResult)
def trigger_semantic_match(body: SemanticMatchRequest):
    """触发语义实体匹配检查。"""
    runner = get_agent_runner()
    try:
        result = runner.run_semantic_match(
            dict_id_a=body.dict_id_a,
            dict_id_b=body.dict_id_b,
            entity_a_name=body.entity_a_name,
            entity_b_name=body.entity_b_name,
            entity_type=body.entity_type,
        )
        return AgentReviewResult(**result)
    except Exception as e:
        logger.error(f"[API] 语义匹配失败: {body.dict_id_a} vs {body.dict_id_b} | {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Agent D: 实体合并消歧 ──────────────────────────────────────────────────

# ── Agent D: 实体合并消歧 ────────────────────────────────────────────────

class BatchMergeRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=200, description="批量消歧数量")


class MergeCandidateItem(BaseModel):
    id: int
    entity_name: str = ""
    entity_type: str = ""
    source_site: str = ""
    source_doc_uuid: str = ""
    document_title: str = ""
    confidence_score: float = 0.0
    entity_data: dict = Field(default_factory=dict)
    dict_id: int | None = None
    dict_name: str = ""


class MergeCandidateListResponse(BaseModel):
    items: list[MergeCandidateItem] = Field(default_factory=list)
    total: int = 0


class MergeCandidateStatsResponse(BaseModel):
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)


class MergeQueueStatusResponse(BaseModel):
    running: bool = False
    pending: int = 0
    completed: int = 0
    failed: int = 0
    total: int = 0


@router.get("/merge/candidates", response_model=MergeCandidateListResponse)
def list_merge_candidates(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    entity_type: str = Query(""),
    source_site: str = Query(""),
    search: str = Query(""),
    conf_min: float = Query(0.0, ge=0.0, le=1.0),
    conf_max: float = Query(1.0, ge=0.0, le=1.0),
    has_dict: bool | None = Query(None),
):
    """列出可消歧实体：已 approved（非 primekg），且未在待审提案中。"""
    from sqlalchemy import text

    with get_session() as session:
        filters = """
            e.review_status IN ('auto_approved', 'approved')
            AND e.source_site != 'primekg'
            AND e.is_deleted = false
            AND e.id NOT IN (
                SELECT source_entity_id FROM entity_identity_merge_proposal
                WHERE status = 'pending'
            )
        """
        params: dict = {}

        if entity_type:
            filters += " AND e.entity_type = :entity_type"
            params["entity_type"] = entity_type
        if source_site:
            filters += " AND e.source_site = :source_site"
            params["source_site"] = source_site
        if search:
            filters += " AND e.entity_name ILIKE :search"
            params["search"] = f"%{search}%"
        if conf_min > 0:
            filters += " AND e.confidence_score >= :conf_min"
            params["conf_min"] = conf_min
        if conf_max < 1.0:
            filters += " AND e.confidence_score <= :conf_max"
            params["conf_max"] = conf_max
        if has_dict is True:
            filters += " AND e.dict_id IS NOT NULL"
        elif has_dict is False:
            filters += " AND e.dict_id IS NULL"

        total_row = session.execute(
            text(f"SELECT COUNT(*) FROM extracted_entities e WHERE {filters}"),
            params,
        ).scalar()

        params["limit"] = page_size
        params["offset"] = (page - 1) * page_size

        rows = session.execute(
            text(f"""
                SELECT e.id, e.entity_name, e.entity_type, e.source_site,
                       e.source_doc_uuid, e.confidence_score, e.entity_data,
                       d.title AS document_title, e.dict_id,
                       ea.name AS dict_name
                FROM extracted_entities e
                LEFT JOIN documents d ON e.source_doc_uuid = d.uuid
                LEFT JOIN entity_dict ed ON e.dict_id = ed.id
                LEFT JOIN entity_aliases ea ON ed.id = ea.dict_id AND ea.is_canonical = true AND ea.is_deleted = false
                WHERE {filters}
                ORDER BY e.confidence_score DESC
                LIMIT :limit OFFSET :offset
            """),            params,
        ).fetchall()

        items = [
            MergeCandidateItem(
                id=row.id,
                entity_name=row.entity_name or "",
                entity_type=row.entity_type or "",
                source_site=row.source_site or "",
                source_doc_uuid=row.source_doc_uuid or "",
                document_title=row.document_title or "",
                confidence_score=float(row.confidence_score or 0),
                entity_data=row.entity_data or {},
                dict_id=row.dict_id,
                dict_name=row.dict_name or "",
            )
            for row in rows
        ]
        return MergeCandidateListResponse(items=items, total=total_row or 0)


@router.get("/merge/candidates/stats", response_model=MergeCandidateStatsResponse)
def get_merge_candidates_stats():
    """消歧候选实体的分布统计。"""
    from sqlalchemy import text

    with get_session() as session:
        base_filter = """
            e.review_status IN ('auto_approved', 'approved')
            AND e.source_site != 'primekg'
            AND e.is_deleted = false
            AND e.id NOT IN (
                SELECT source_entity_id FROM entity_identity_merge_proposal
                WHERE status = 'pending'
            )
        """

        total = session.execute(
            text(f"SELECT COUNT(*) FROM extracted_entities e WHERE {base_filter}")
        ).scalar() or 0

        type_rows = session.execute(
            text(f"""
                SELECT e.entity_type, COUNT(*) AS cnt
                FROM extracted_entities e WHERE {base_filter}
                GROUP BY e.entity_type ORDER BY cnt DESC
            """)
        ).fetchall()
        by_type = {r.entity_type or "(空)": r.cnt for r in type_rows}

        source_rows = session.execute(
            text(f"""
                SELECT e.source_site, COUNT(*) AS cnt
                FROM extracted_entities e WHERE {base_filter}
                GROUP BY e.source_site ORDER BY cnt DESC
            """)
        ).fetchall()
        by_source = {r.source_site or "(空)": r.cnt for r in source_rows}

        return MergeCandidateStatsResponse(total=total, by_type=by_type, by_source=by_source)


@router.post("/merge/entity/{entity_id}", response_model=AgentReviewResult)
def trigger_entity_merge(entity_id: int):
    """对单个 extracted_entity 执行合并消歧——同步执行，直接返回结果。（兼容旧版，新前端用 SSE）"""
    with get_session() as session:
        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")

    runner = get_agent_runner()
    try:
        result = runner.run_entity_merge(entity_id)
        return AgentReviewResult(**result)
    except Exception as e:
        logger.error(f"[API] 实体合并消歧失败: entity_id={entity_id} | {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/merge/entity/{entity_id}/stream")
async def stream_entity_merge(entity_id: int):
    """SSE 流式端点：实时推送消歧 Agent 的每一步推理和最终结果。

    Event types:
        status  — 状态变化（loading / running / finalizing）
        step    — Agent 单步推理或工具调用
        result  — 最终决策（linked / escalated）
        error   — 出错
        done    — 流结束
    """
    with get_session() as session:
        row = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="实体不存在")

    async def generate():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def run():
            try:
                runner = get_agent_runner()
                for event in runner.run_entity_merge_stream(entity_id):
                    asyncio.run_coroutine_threadsafe(q.put(event), loop)
            except Exception as e:
                logger.error(f"[SSE] 消歧流异常: entity_id={entity_id} | {e}")
                asyncio.run_coroutine_threadsafe(
                    q.put({"type": "error", "message": str(e)}),
                    loop,
                )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        while True:
            event = await q.get()
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/merge/batch", response_model=MergeQueueStatusResponse)
def trigger_batch_merge(body: BatchMergeRequest):
    """批量消歧——从可消歧实体中取 N 个入队，后台消费。"""
    from core_engine.merge_queue import get_merge_queue
    from sqlalchemy import text

    mq = get_merge_queue()

    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT e.id FROM extracted_entities e
                WHERE e.review_status IN ('auto_approved', 'approved')
                  AND e.source_site != 'primekg'
                  AND e.is_deleted = false
                  AND e.id NOT IN (
                      SELECT source_entity_id FROM entity_identity_merge_proposal
                      WHERE status = 'pending'
                  )
                ORDER BY e.confidence_score DESC
                LIMIT :limit
            """),
            {"limit": body.count},
        ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="无可消歧实体")

    entity_ids = [r.id for r in rows]
    pending = mq.enqueue(entity_ids)
    logger.info(f"[API] 批量消歧入队: {len(entity_ids)} 条, 队列待处理: {pending}")

    return MergeQueueStatusResponse(**mq.status())


@router.get("/merge/queue/status", response_model=MergeQueueStatusResponse)
def get_merge_queue_status():
    """查询消歧队列状态。"""
    from core_engine.merge_queue import get_merge_queue
    mq = get_merge_queue()
    return MergeQueueStatusResponse(**mq.status())


# ── 运行记录查询 ───────────────────────────────────────────────────────────

@router.get("/run/{run_id}")
def get_run(run_id: str):
    """查询单条 Agent 运行记录的详细推理步骤。"""
    record = get_agent_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return record


@router.get("/runs", response_model=RunListResponse)
def list_runs(
    agent_type: str = Query("", description="entity_review | relation_review | conflict_resolution | semantic_match | entity_merge"),
    target_id: int = Query(0, description="可选，查询特定实体的运行记录"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    """分页查询 Agent 运行历史。target_id 可指定实体/关系 ID。"""
    items, total = list_agent_runs(agent_type=agent_type, target_id=target_id, page=page, page_size=page_size)
    return RunListResponse(total=total, items=items)


@router.get("/runs/by-document", response_model=DocumentRunListResponse)
def list_runs_grouped_by_document(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    """按文档维度查询实体审核记录 — 聚合展示每篇文档的审核统计 + 可展开的实体列表。"""
    items, total = list_runs_by_document(page=page, page_size=page_size)
    return DocumentRunListResponse(
        total=total,
        items=[DocumentRunSummary(**d) for d in items],
    )


@router.get("/stats", response_model=AgentStatsResponse)
def get_stats():
    """Agent 运行统计。"""
    return AgentStatsResponse(stats=get_agent_stats())


@router.get("/pipeline/overview", response_model=PipelineOverviewResponse)
def get_pipeline_overview():
    """流程总览：抽取 → 审核 → 消歧 → 归并 → 同步 各阶段数据与状态分布。"""
    from sqlalchemy import text

    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        stages: list[PipelineStage] = []

        # ① 抽取
        rows = session.execute(text(
            "SELECT extraction_status, COUNT(*) FROM documents WHERE is_deleted=false GROUP BY extraction_status"
        )).fetchall()
        by = {r[0] or "unknown": r[1] for r in rows}
        site_rows = session.execute(text(
            "SELECT extraction_status, source_site, COUNT(*) FROM documents WHERE is_deleted=false "
            "AND extraction_status IN ('pending','done') "
            "GROUP BY extraction_status, source_site ORDER BY COUNT(*) DESC"
        )).fetchall()
        by_site: dict[str, dict[str, int]] = {}
        for st, site, cnt in site_rows:
            by_site.setdefault(st, {})[site or "(空)"] = cnt
        stages.append(PipelineStage(key="extraction", label="抽取", total=sum(by.values()), by_status=by, by_site=by_site))

        # ② 实体审核
        rows = session.execute(text(
            "SELECT review_status, COUNT(*) FROM extracted_entities WHERE is_deleted=false GROUP BY review_status"
        )).fetchall()
        by = {r[0] or "unknown": r[1] for r in rows}
        site_rows = session.execute(text(
            "SELECT source_site, COUNT(*) FROM extracted_entities WHERE is_deleted=false AND review_status='pending' "
            "GROUP BY source_site ORDER BY COUNT(*) DESC"
        )).fetchall()
        by_site = {"pending": {r[0] or "(空)": r[1] for r in site_rows}}
        stages.append(PipelineStage(key="entity_review", label="实体审核", total=sum(by.values()), by_status=by, by_site=by_site))

        # ③ 关系审核
        rows = session.execute(text(
            "SELECT review_status, COUNT(*) FROM entity_relations WHERE is_deleted=false GROUP BY review_status"
        )).fetchall()
        by = {r[0] or "unknown": r[1] for r in rows}
        stages.append(PipelineStage(key="relation_review", label="关系审核", total=sum(by.values()), by_status=by))

        # ④ 消歧链接（dict_id 是否命中）
        # 注意：extracted_entities 中混入了 source_site='primekg' 的本体镜像行，
        # 它们由实体字典 1:1 镜像、不参与消歧（同文件 .list_discriminable_entities 亦排除 primekg）。
        # 为避免「未消歧(none)」等指标被本体规模污染，以下统计统一排除 primekg；
        # 用 (source_site IS NULL OR source_site != 'primekg') 显式保留空 source_site 的存量实体，仅排除本体行。
        _primekg_excl = "AND (source_site IS NULL OR source_site != 'primekg')"
        linked = session.execute(text(
            "SELECT COUNT(*) FROM extracted_entities WHERE is_deleted=false "
            f"AND dict_id IS NOT NULL {_primekg_excl}"
        )).scalar() or 0
        unlinked = session.execute(text(
            "SELECT COUNT(*) FROM extracted_entities WHERE is_deleted=false "
            f"AND dict_id IS NULL {_primekg_excl}"
        )).scalar() or 0
        match_rows = session.execute(text(
            "SELECT entity_data->'_disambig'->>'match_step' AS ms, COUNT(*) "
            f"FROM extracted_entities WHERE is_deleted=false {_primekg_excl} GROUP BY ms"
        )).fetchall()
        by_match_step = {r[0] or "none": r[1] for r in match_rows}
        stages.append(PipelineStage(
            key="disambiguation", label="消歧链接", total=linked + unlinked,
            by_status={"linked": linked, "unlinked": unlinked},
            by_match_step=by_match_step,
        ))

        # ⑤ 归并
        prop_rows = session.execute(text(
            "SELECT status, COUNT(*) FROM entity_identity_merge_proposal GROUP BY status"
        )).fetchall()
        conflicts = session.execute(text(
            "SELECT COUNT(*) FROM entity_merge_queue WHERE resolution='pending'"
        )).scalar() or 0
        by = {f"proposal_{r[0] or 'unknown'}": r[1] for r in prop_rows}
        by["conflict_pending"] = conflicts

        # 字典现状（entity_dict 总量 + 强 ID 重复组数）
        dict_rows = session.execute(text(
            "SELECT is_verified, COUNT(*) FROM entity_dict WHERE is_deleted=false GROUP BY is_verified"
        )).fetchall()
        by_dict = {("dict_verified" if r[0] else "dict_unverified"): r[1] for r in dict_rows}
        dup_groups = session.execute(text(
            "SELECT COUNT(*) FROM ("
            "  SELECT etype, sid FROM ("
            "    SELECT 'ClinicalTrial' AS etype, external_ids->>'trial_id' AS sid"
            "      FROM entity_dict WHERE is_deleted=false AND entity_type='ClinicalTrial' AND external_ids ? 'trial_id'"
            "    UNION ALL"
            "    SELECT 'ApprovalEvent', external_ids->>'approval_number'"
            "      FROM entity_dict WHERE is_deleted=false AND entity_type='ApprovalEvent' AND external_ids ? 'approval_number'"
            "    UNION ALL"
            "    SELECT 'Drug', external_ids->>'cas_number'"
            "      FROM entity_dict WHERE is_deleted=false AND entity_type='Drug' AND external_ids ? 'cas_number'"
            "    UNION ALL"
            "    SELECT 'Compound', external_ids->>'cas_number'"
            "      FROM entity_dict WHERE is_deleted=false AND entity_type='Compound' AND external_ids ? 'cas_number'"
            "    UNION ALL"
            "    SELECT 'Company', external_ids->>'stock_code'"
            "      FROM entity_dict WHERE is_deleted=false AND entity_type='Company' AND external_ids ? 'stock_code'"
            # 注：Indication/icd_code 已于 2026-09-28 移出强 ID（分类码粒度不足），
            #     此统计须与 disambiguation/rules.py:STRONG_ID_FIELDS 保持一致。
            "    UNION ALL"
            "    SELECT 'Pathway', external_ids->>'id'"
            "      FROM entity_dict WHERE is_deleted=false AND entity_type='Pathway' AND external_ids ? 'id'"
            "  ) strong WHERE sid IS NOT NULL AND sid != ''"
            "  GROUP BY etype, sid HAVING COUNT(*) > 1"
            ") t"
        )).scalar() or 0
        by_dict["dict_duplicate_groups"] = dup_groups

        stages.append(PipelineStage(
            key="merge", label="归并", total=sum(by.values()),
            by_status=by, by_dict=by_dict,
        ))

        # ⑥ 同步（正文索引发布 + 实体推送 + 关系推送）
        from db import index_publication
        ent_rows = session.execute(text(
            "SELECT weknora_graph_status, COUNT(*) FROM extracted_entities WHERE is_deleted=false GROUP BY weknora_graph_status"
        )).fetchall()
        rel_rows = session.execute(text(
            "SELECT weknora_graph_status, COUNT(*) FROM entity_relations WHERE is_deleted=false GROUP BY weknora_graph_status"
        )).fetchall()
        by = {f"index_{state}": n for state, n in index_publication.state_counts().items()}
        for r in ent_rows:
            by[f"graph_entity_{r[0] or 'unknown'}"] = r[1]
        for r in rel_rows:
            by[f"graph_relation_{r[0] or 'unknown'}"] = r[1]
        stages.append(PipelineStage(key="sync", label="同步", total=sum(by.values()), by_status=by))

    return PipelineOverviewResponse(stages=stages)


@router.get("/entity-review-stats", response_model=EntityReviewStatsResponse)
def get_entity_review_stats():
    """实体审核存量统计：待审核实体数 + 已审核实体数。"""
    from sqlalchemy import text

    with get_session() as session:
        if session is None:
            raise HTTPException(status_code=503, detail="数据库未连接")

        pending = session.execute(text(
            "SELECT COUNT(*) FROM extracted_entities WHERE is_deleted=false AND review_status='pending'"
        )).scalar() or 0
        reviewed = session.execute(text(
            "SELECT COUNT(*) FROM extracted_entities WHERE is_deleted=false "
            "AND review_status IN ('approved','auto_approved','rejected')"
        )).scalar() or 0

    return EntityReviewStatsResponse(pending_entities=pending, reviewed_entities=reviewed)
