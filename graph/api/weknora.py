"""审核图推 WeKnora 图谱。文档正文进 WeKnora 只走 store/index_publication（经桥接服务），这里不再有第二条路。"""
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from graph.api.schemas.request import FullGraphSyncRequest, GraphSyncRequest
from graph.api.schemas.response import GraphSyncResponse, GraphSyncStatsResponse, GraphSyncStatusCount
from graph.db.repository import (
    batch_mark_entity_dict_failed,
    batch_mark_entity_dict_synced,
    batch_mark_entity_weknora_graph_failed,
    batch_mark_entity_weknora_graph_pushed,
    batch_mark_relation_weknora_graph_failed,
    batch_mark_relation_weknora_graph_pushed,
    get_entities_for_weknora_graph,
    get_entity_dict_for_weknora,
    get_relations_for_weknora_graph,
    get_weknora_graph_stats,
)
from graph.publish.weknora_graph_client import get_weknora_graph_client

router = APIRouter(prefix="/api/weknora", tags=["weknora"])



BATCH_SIZE = 50
MAX_CONCURRENCY = 5


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
