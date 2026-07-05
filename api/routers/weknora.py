"""WeKnora 同步接口 — 文档同步 + 图数据同步"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from api.schemas.request import WeknoraBatchRequest, GraphSyncRequest, FullGraphSyncRequest
from api.schemas.response import (
    WeknoraPendingResponse, WeknoraPendingItem,
    WeknoraSyncResponse, WeknoraBatchResponse, WeknoraStatsResponse,
    GraphSyncResponse, GraphSyncStatsResponse, GraphSyncStatusCount,
)
from db.repository import (
    update_weknora_sync_status, get_pending_weknora_sync,
    get_entity_dict_for_weknora, batch_mark_entity_dict_synced, batch_mark_entity_dict_failed,
    get_entities_for_weknora_graph, batch_mark_entity_weknora_graph_pushed, batch_mark_entity_weknora_graph_failed,
    get_relations_for_weknora_graph, batch_mark_relation_weknora_graph_pushed, batch_mark_relation_weknora_graph_failed,
    get_weknora_graph_stats,
)
from db.session import get_session
from db.models import Document
from sqlalchemy import func
from core_engine.weknora_graph_client import get_weknora_graph_client

router = APIRouter(prefix="/api/weknora", tags=["weknora"])


# ═══════════════════════════════════════════════════════════════════════════
# 文档同步（Markdown 全文）
# ═══════════════════════════════════════════════════════════════════════════

def _build_source_metadata(doc: dict) -> dict:
    """从文档字段构建 WeKnora source_metadata。"""
    meta = {
        "source_uuid": doc.get("uuid", ""),
        "parent_uuid": doc.get("parent_uuid", ""),
        "doc_type": doc.get("doc_type", ""),
        "source_site": doc.get("source_site", ""),
        "source_url": doc.get("source_url", ""),
        "published_at": doc.get("publish_date", ""),
        "language": doc.get("language", ""),
        "confidence_score": doc.get("confidence_score", 0.0),
        "parse_method": doc.get("parse_method", ""),
        "crawl_timestamp": doc.get("crawl_timestamp", ""),
    }
    return {k: v for k, v in meta.items() if v not in ("", [], None, 0.0)}


BATCH_SIZE = 50
MAX_CONCURRENCY = 5


def _sync_one_row(row: dict, kb_id: str = "") -> tuple[str, bool, str]:
    """同步单行（dict）到 WeKnora，不再查 DB。返回 (uuid, success, message)。"""
    uuid = row.get("uuid", "")
    title = row.get("title") or uuid
    content = row.get("raw_content") or ""
    if not content.strip():
        return uuid, False, f"文档内容为空: {uuid}"

    metadata = _build_source_metadata({
        "uuid": uuid,
        "parent_uuid": row.get("parent_uuid", ""),
        "doc_type": row.get("doc_type", ""),
        "source_site": row.get("source_site", ""),
        "source_url": row.get("source_url", ""),
        "publish_date": row.get("publish_date", ""),
        "language": row.get("language", ""),
        "confidence_score": row.get("confidence_score", 0.0),
        "parse_method": row.get("parse_method", ""),
        "crawl_timestamp": str(row.get("crawl_timestamp") or ""),
    })
    client = get_weknora_graph_client()
    weknora_id = client.upload_document(title, content, metadata, kb_id)
    update_weknora_sync_status(uuid, "synced")
    return uuid, True, weknora_id or "synced"


def _sync_one_document(uuid: str, kb_id: str = "") -> tuple[bool, str]:
    """同步单篇文档到 WeKnora（单条接口用，需查 DB）。返回 (success, message)。"""
    from db.repository import get_document_by_uuid

    doc = get_document_by_uuid(uuid)
    if doc is None:
        return False, f"文档不存在: {uuid}"

    row = {
        "uuid": doc.uuid,
        "title": doc.title,
        "raw_content": doc.raw_content,
        "parent_uuid": doc.parent_uuid,
        "doc_type": doc.doc_type,
        "source_site": doc.source_site,
        "source_url": doc.source_url,
        "publish_date": doc.publish_date,
        "language": doc.language,
        "confidence_score": doc.confidence_score,
        "parse_method": doc.parse_method,
        "crawl_timestamp": doc.crawl_timestamp,
    }
    _, success, msg = _sync_one_row(row, kb_id)
    return success, msg


@router.get("/pending", response_model=WeknoraPendingResponse)
def get_pending(
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source_site: str = Query("", description="按来源站点过滤，留空则查全部"),
):
    """待同步文档列表查询（分页）"""
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit 必须在 1-500 之间")

    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

    try:
        rows, total = get_pending_weknora_sync(limit=limit, offset=offset, source_site=source_site)
        items = [
            WeknoraPendingItem(
                uuid=r["uuid"] or "",
                title=r["title"] or "",
                source_site=r["source_site"] or "",
                publish_date=r["publish_date"] or "",
            )
            for r in rows
        ]

        logger.info(f"[API] 待同步文档查询: limit={limit}, offset={offset}, total={total}, returned={len(items)}")
        return WeknoraPendingResponse(total=total, items=items)

    except Exception as e:
        logger.error(f"[API] 待同步文档查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


@router.post("/sync/{uuid}", response_model=WeknoraSyncResponse)
def sync_single(uuid: str, kb_id: str = Query("", description="目标知识库 ID，留空用 .env 配置")):
    """手动触发单篇文档同步到 WeKnora。"""
    if not uuid or not uuid.strip():
        raise HTTPException(status_code=400, detail="uuid 不能为空")

    # 若用户指定了知识库 ID，先校验是否存在
    if kb_id:
        try:
            get_weknora_graph_client().validate_knowledge_base(kb_id)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        success, msg = _sync_one_document(uuid, kb_id)
        if not success:
            raise HTTPException(status_code=400, detail=msg)
        logger.info(f"[API] WeKnora 同步成功: {uuid}")
        return WeknoraSyncResponse(success=True, message=msg)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] WeKnora 同步失败: {uuid} | {e}")
        try:
            update_weknora_sync_status(uuid, "failed")
        except Exception as se:
            logger.warning(f"[API] 更新同步状态失败 uuid={uuid}: {se}")
        return WeknoraSyncResponse(success=False, message=str(e))


@router.post("/sync-batch", response_model=WeknoraBatchResponse)
def sync_batch(
    body: WeknoraBatchRequest,
    kb_id: str = Query("", description="目标知识库 ID，留空用 .env 配置"),
    source_site: str = Query("", description="按来源站点过滤同步"),
):
    """批量同步 pending 文档到 WeKnora。后端内部自动分批，前端只管传总量。"""
    if body.limit < 1:
        raise HTTPException(status_code=400, detail="limit 必须 >= 1")

    if kb_id:
        try:
            get_weknora_graph_client().validate_knowledge_base(kb_id)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        # 先取第一页确定有数据
        rows, total = get_pending_weknora_sync(limit=min(body.limit, BATCH_SIZE), offset=0, source_site=source_site)
        if not rows:
            return WeknoraBatchResponse(triggered=0)

        total_to_sync = min(body.limit, total)
        logger.info(f"[API] WeKnora 批量同步: 请求 {body.limit} 篇, pending 共 {total} 篇, 实际同步 {total_to_sync} 篇")

        def _bg_sync():
            synced = 0
            while synced < total_to_sync:
                batch_rows, _ = get_pending_weknora_sync(
                    limit=BATCH_SIZE, offset=0, source_site=source_site,
                )
                if not batch_rows:
                    break

                with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
                    futures = {pool.submit(_sync_one_row, row, kb_id): row for row in batch_rows}
                    for future in as_completed(futures):
                        try:
                            uuid, success, msg = future.result()
                            if success:
                                synced += 1
                        except Exception as e:
                            logger.error(f"[API] 后台同步异常: {e}")

            logger.info(f"[API] WeKnora 批量同步完成: {synced}/{total_to_sync}")

        threading.Thread(target=_bg_sync, daemon=True).start()
        return WeknoraBatchResponse(triggered=total_to_sync)

    except Exception as e:
        logger.error(f"[API] WeKnora 批量同步失败: {e}")
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


@router.get("/stats", response_model=WeknoraStatsResponse)
def get_stats():
    """WeKnora 文档同步状态统计"""
    with get_session() as session:
        if session is None:
            logger.error("[API] 数据库连接失败")
            raise HTTPException(status_code=503, detail="数据库未连接")

        try:
            def _count(status: str) -> int:
                return session.query(func.count(Document.id)).filter(
                    Document.weknora_sync_status == status,
                    Document.is_deleted == False,
                ).scalar()

            stats = WeknoraStatsResponse(
                pending=_count("pending"),
                synced=_count("synced"),
                failed=_count("failed"),
            )

            logger.info(f"[API] WeKnora 同步统计: {stats.json()}")
            return stats

        except Exception as e:
            logger.error(f"[API] WeKnora 同步统计失败: {e}")
            raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")


# ═══════════════════════════════════════════════════════════════════════════
# 图数据同步（entity_dict / entities / relations → WeKnora → Neo4j）
# ═══════════════════════════════════════════════════════════════════════════

GRAPH_BATCH_SIZE = 200


def _do_sync_entity_dict(kb_id: str = "") -> tuple[int, int, str]:
    """将 entity_dict 推送到 WeKnora 镜像表，并自动初始化 PrimeKG 副本。内部自动分页。"""
    client = get_weknora_graph_client()
    total_pushed = 0
    total_failed = 0

    while True:
        rows = get_entity_dict_for_weknora(limit=GRAPH_BATCH_SIZE)
        if not rows:
            break

        batch_ids = [row["id"] for row in rows]
        try:
            result = client.batch_upsert_entity_dict(rows)
            upserted = result.get("data", {}).get("upserted", 0)
            total_pushed += upserted
            batch_mark_entity_dict_synced(batch_ids)
        except Exception as batch_error:
            total_failed += len(rows)
            batch_mark_entity_dict_failed(batch_ids, str(batch_error))
            logger.error(f"[API] entity_dict 批次失败: {batch_error}")

    logger.info(f"[API] entity_dict 同步完成: pushed={total_pushed}, failed={total_failed}")

    if kb_id and total_pushed > 0:
        try:
            _, _, init_msg = _do_init_copies(kb_id)
            logger.info(f"[API] auto init_copies: {init_msg}")
        except Exception as e:
            logger.error(f"[API] auto init_copies 失败: {e}")

    return total_pushed, total_failed, f"entity_dict={total_pushed}/{total_failed}"


def _do_init_copies(kb_id: str) -> tuple[int, int, str]:
    """触发 WeKnora 从 entity_dict 镜像表初始化 PrimeKG 副本到 graph_entities。"""
    client = get_weknora_graph_client()
    result = client.init_copies(kb_id)
    upserted = result.get("data", {}).get("upserted", 0)
    return upserted, 0, "PrimeKG 副本初始化完成"


def _do_sync_entities(kb_id: str) -> tuple[int, int, str]:
    """将审核通过的抽取实体推送到 WeKnora graph_entities。内部自动分页。"""
    client = get_weknora_graph_client()
    total_pushed = 0
    total_failed = 0

    while True:
        entities = get_entities_for_weknora_graph(limit=GRAPH_BATCH_SIZE)
        if not entities:
            break

        batch_ids = [ent["id"] for ent in entities]
        try:
            result = client.batch_upsert_entities(entities, kb_id)
            pushed = result.get("data", {}).get("upserted", 0)
            total_pushed += pushed
            batch_mark_entity_weknora_graph_pushed(batch_ids)
        except Exception as batch_error:
            total_failed += len(entities)
            batch_mark_entity_weknora_graph_failed(batch_ids, str(batch_error))
            logger.error(f"[API] sync-entities 批次失败: {batch_error}")

    logger.info(f"[API] sync-entities 完成: pushed={total_pushed}, failed={total_failed}")
    return total_pushed, total_failed, f"同步完成: pushed={total_pushed}, failed={total_failed}"


def _do_sync_relations(kb_id: str) -> tuple[int, int, str]:
    """将审核通过的抽取关系推送到 WeKnora graph_relations。内部自动分页。"""
    client = get_weknora_graph_client()
    total_pushed = 0
    total_failed = 0

    while True:
        relations = get_relations_for_weknora_graph(limit=GRAPH_BATCH_SIZE)
        if not relations:
            break

        batch_ids = [rel["id"] for rel in relations]
        try:
            result = client.batch_upsert_relations(relations, kb_id)
            pushed = result.get("data", {}).get("upserted", 0)
            total_pushed += pushed
            batch_mark_relation_weknora_graph_pushed(batch_ids)
        except Exception as batch_error:
            total_failed += len(relations)
            batch_mark_relation_weknora_graph_failed(batch_ids, str(batch_error))
            logger.error(f"[API] sync-relations 批次失败: {batch_error}")

    logger.info(f"[API] 关系图同步完成: pushed={total_pushed}, failed={total_failed}")
    return total_pushed, total_failed, f"同步完成: pushed={total_pushed}, failed={total_failed}"


# ── 单独同步端点 ────────────────────────────────────────────────────────

@router.post("/graph/sync-entity-dict", response_model=GraphSyncResponse)
def sync_entity_dict_to_weknora(body: GraphSyncRequest, kb_id: str = Query("", description="目标知识库 ID，留空则跳过 init_copies")):
    """将 entity_dict 推送到 WeKnora 镜像表，并自动初始化 PrimeKG 副本（若提供 kb_id）。"""
    try:
        pushed, failed, message = _do_sync_entity_dict(kb_id)
        return GraphSyncResponse(success=True, pushed=pushed, failed=failed, message=message)
    except Exception as e:
        logger.error(f"[API] entity_dict 同步失败: {e}")
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


@router.post("/graph/init-copies", response_model=GraphSyncResponse)
def init_primekg_copies(kb_id: str = Query("", description="目标知识库 ID，留空用 .env 配置")):
    """触发 WeKnora 从 entity_dict 镜像表初始化 PrimeKG 副本到 graph_entities。"""
    try:
        pushed, failed, message = _do_init_copies(kb_id)
        return GraphSyncResponse(success=True, pushed=pushed, failed=failed, message=message)
    except Exception as e:
        logger.error(f"[API] init_copies 失败: {e}")
        raise HTTPException(status_code=500, detail=f"初始化失败: {str(e)}")


@router.post("/graph/sync-entities", response_model=GraphSyncResponse)
def sync_entities_to_weknora_graph(kb_id: str = Query("", description="目标知识库 ID，留空用 .env 配置")):
    """将审核通过的抽取实体推送到 WeKnora graph_entities。"""
    try:
        pushed, failed, message = _do_sync_entities(kb_id)
        return GraphSyncResponse(success=True, pushed=pushed, failed=failed, message=message)
    except Exception as e:
        logger.error(f"[API] sync-entities 异常: {e}")
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


@router.post("/graph/sync-relations", response_model=GraphSyncResponse)
def sync_relations_to_weknora_graph(kb_id: str = Query("", description="目标知识库 ID，留空用 .env 配置")):
    """将审核通过的抽取关系推送到 WeKnora graph_relations。"""
    try:
        pushed, failed, message = _do_sync_relations(kb_id)
        return GraphSyncResponse(success=True, pushed=pushed, failed=failed, message=message)
    except Exception as e:
        logger.error(f"[API] 关系图同步失败: {e}")
        raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")


# ── 全量同步 ────────────────────────────────────────────────────────────

@router.post("/graph/sync-all", response_model=GraphSyncResponse)
def sync_all_to_weknora(body: FullGraphSyncRequest):
    """全量同步：依次执行 entity_dict → init_copies → entities → relations。

    顺序依赖：
    1. entity_dict 镜像表（全局，无需 kb_id）
    2. init_copies 从镜像表初始化 PrimeKG 副本（需要 kb_id）
    3. 抽取实体推送到 graph_entities（需要 kb_id）
    4. 抽取关系推送到 graph_relations（需要 kb_id，依赖实体已存在）
    """
    kb_id = body.kb_id
    steps = [
        ("entity_dict", lambda: _do_sync_entity_dict(kb_id)),
        ("entities", lambda: _do_sync_entities(kb_id)),
        ("relations", lambda: _do_sync_relations(kb_id)),
    ]

    results: dict[str, dict] = {}
    total_pushed = 0
    total_failed = 0

    for step_name, step_fn in steps:
        try:
            pushed, failed, msg = step_fn()
            results[step_name] = {"pushed": pushed, "failed": failed, "message": msg}
            total_pushed += pushed
            total_failed += failed
            logger.info(f"[API] sync-all 步骤 {step_name}: pushed={pushed}, failed={failed}")
        except Exception as e:
            results[step_name] = {"pushed": 0, "failed": 0, "message": str(e)}
            total_failed += 1
            logger.error(f"[API] sync-all 步骤 {step_name} 异常: {e}")
            # 关系步骤失败不阻断，但实体步骤失败则跳过关系
            if step_name == "entities":
                results["relations"] = {"pushed": 0, "failed": 0, "message": "实体同步失败，跳过关系同步"}
                break

    parts = []
    for k, v in results.items():
        parts.append("{}={}/{}".format(k, v["pushed"], v["failed"]))
    message = "全量同步完成: {}".format(", ".join(parts))

    return GraphSyncResponse(
        success=True,
        pushed=total_pushed,
        failed=total_failed,
        message=message,
    )


@router.get("/graph/stats", response_model=GraphSyncStatsResponse)
def get_graph_sync_stats():
    """WeKnora 图同步状态统计"""
    try:
        stats = get_weknora_graph_stats()
        return GraphSyncStatsResponse(
            entity_dict=GraphSyncStatusCount(
                pending=stats["entity_dict"]["pending"],
                synced=stats["entity_dict"]["synced"],
                failed=stats["entity_dict"]["failed"],
            ),
            entities=GraphSyncStatusCount(
                pending=stats["entities"]["pending"],
                pushed=stats["entities"]["pushed"],
                failed=stats["entities"]["failed"],
            ),
            relations=GraphSyncStatusCount(
                pending=stats["relations"]["pending"],
                pushed=stats["relations"]["pushed"],
                failed=stats["relations"]["failed"],
            ),
        )
    except Exception as e:
        logger.error(f"[API] 图同步统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"统计失败: {str(e)}")
