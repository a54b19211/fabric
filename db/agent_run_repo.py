# -*- coding: utf-8 -*-
"""agent_run 表 CRUD — Agent 运行记录持久化。"""

from datetime import datetime, timezone
import json

from loguru import logger
from sqlalchemy import text

from db.session import get_session


def save_agent_run(
    run_id: str,
    agent_type: str,
    target_id: int,
    decision: str = "",
    reason: str = "",
    confidence: float = 0.0,
    steps: list[dict] | None = None,
    entity_name: str = "",
    document_title: str = "",
    document_uuid: str = "",
    batch_id: str = "",
) -> int:
    """插入一条 Agent 运行记录。返回新记录的 id。"""
    try:
        with get_session() as session:
            result = session.execute(
                text("""
                    INSERT INTO agent_run (run_id, agent_type, target_id, status,
                                           decision, reason, confidence, steps,
                                           entity_name, document_title, document_uuid, batch_id, started_at)
                    VALUES (:run_id, :agent_type, :target_id, 'completed',
                            :decision, :reason, :confidence, :steps,
                            :entity_name, :document_title, :document_uuid, :batch_id, :started_at)
                    RETURNING id
                """),
                {
                    "run_id": run_id,
                    "agent_type": agent_type,
                    "target_id": target_id,
                    "decision": decision or "",
                    "reason": reason or "",
                    "confidence": confidence,
                    "steps": json.dumps(steps or [], ensure_ascii=False),
                    "entity_name": entity_name or "",
                    "document_title": document_title or "",
                    "document_uuid": document_uuid or "",
                    "batch_id": batch_id or "",
                    "started_at": datetime.now(timezone.utc),
                },
            )
            session.commit()
            row = result.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"[agent_run_repo] 保存失败: {e}")
        return 0


def update_agent_run(run_id: str, **kwargs):
    """更新 Agent 运行记录。"""
    if not kwargs:
        return
    try:
        sets = ", ".join(f"{k} = :{k}" for k in kwargs)
        with get_session() as session:
            session.execute(
                text(f"UPDATE agent_run SET {sets} WHERE run_id = :run_id"),
                {"run_id": run_id, **kwargs},
            )
            session.commit()
    except Exception as e:
        logger.error(f"[agent_run_repo] 更新失败: {e}")


def update_agent_run_batch(run_ids: list[str], batch_id: str, document_uuid: str):
    """批量回写 batch_id 和 document_uuid 到已存在的 run 记录。"""
    if not run_ids:
        return
    try:
        with get_session() as session:
            ids_str = ", ".join(f":id_{i}" for i in range(len(run_ids)))
            params = {f"id_{i}": rid for i, rid in enumerate(run_ids)}
            params["batch_id"] = batch_id
            params["document_uuid"] = document_uuid
            session.execute(
                text(
                    f"UPDATE agent_run SET batch_id = :batch_id, document_uuid = :document_uuid "
                    f"WHERE run_id IN ({ids_str})"
                ),
                params,
            )
            session.commit()
    except Exception as e:
        logger.error(f"[agent_run_repo] 批量更新失败: {e}")


def get_agent_run(run_id: str) -> dict | None:
    """查询单条运行记录。"""
    try:
        with get_session() as session:
            row = session.execute(
                text("SELECT * FROM agent_run WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).fetchone()
            if row is None:
                return None
            return _row_to_dict(row)
    except Exception as e:
        logger.error(f"[agent_run_repo] 查询失败: {e}")
        return None


def list_agent_runs(
    agent_type: str = "",
    target_id: int = 0,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """分页查询运行记录。"""
    try:
        with get_session() as session:
            conditions = []
            params = {}
            if agent_type:
                conditions.append("agent_type = :agent_type")
                params["agent_type"] = agent_type
            if target_id:
                conditions.append("target_id = :target_id")
                params["target_id"] = target_id

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

            total = session.execute(
                text(f"SELECT COUNT(*) FROM agent_run {where}"), params
            ).scalar()

            rows = session.execute(
                text(
                    f"SELECT * FROM agent_run {where} "
                    f"ORDER BY created_at DESC "
                    f"OFFSET :offset LIMIT :limit"
                ),
                {**params, "offset": (page - 1) * page_size, "limit": page_size},
            ).fetchall()

            return ([_row_to_dict(r) for r in rows], total)
    except Exception as e:
        logger.error(f"[agent_run_repo] 列表查询失败: {e}")
        return ([], 0)


def list_runs_by_document(page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """按文档分组查询实体审核运行记录。返回聚合后的文档级列表。"""
    try:
        with get_session() as session:
            # 先按 document_uuid 分组计数
            count_sql = """
                SELECT COUNT(DISTINCT COALESCE(NULLIF(document_uuid, ''), 'no-doc-' || target_id::text))
                FROM agent_run
                WHERE agent_type = 'entity_review'
            """
            total = session.execute(text(count_sql)).scalar() or 0

            rows = session.execute(
                text("""
                    SELECT
                        COALESCE(NULLIF(document_uuid, ''), 'no-doc-' || target_id::text) AS doc_key,
                        MAX(document_title) AS document_title,
                        MAX(document_uuid) AS document_uuid,
                        COUNT(*) AS entity_count,
                        COUNT(CASE WHEN decision = 'approved' THEN 1 END) AS approved_count,
                        COUNT(CASE WHEN decision = 'rejected' THEN 1 END) AS rejected_count,
                        COUNT(CASE WHEN decision = 'escalated' THEN 1 END) AS escalated_count,
                        COUNT(CASE WHEN decision = 'error' THEN 1 END) AS error_count,
                        AVG(confidence) AS avg_confidence,
                        MAX(created_at) AS last_review_at
                    FROM agent_run
                    WHERE agent_type = 'entity_review'
                    GROUP BY doc_key
                    ORDER BY last_review_at DESC
                    OFFSET :offset LIMIT :limit
                """),
                {"offset": (page - 1) * page_size, "limit": page_size},
            ).fetchall()

            docs = []
            for r in rows:
                # 查询该文档下的具体实体审核条目
                doc_uuid = r.document_uuid
                entity_rows = []
                if doc_uuid:
                    entity_rows = session.execute(
                        text("""
                            SELECT id, run_id, target_id, entity_name, decision, reason, confidence, created_at
                            FROM agent_run
                            WHERE agent_type = 'entity_review' AND document_uuid = :doc_uuid
                            ORDER BY created_at DESC
                            LIMIT 50
                        """),
                        {"doc_uuid": doc_uuid},
                    ).fetchall()

                docs.append({
                    "document_uuid": doc_uuid or "",
                    "document_title": r.document_title or "",
                    "entity_count": r.entity_count,
                    "approved_count": r.approved_count,
                    "rejected_count": r.rejected_count,
                    "escalated_count": r.escalated_count,
                    "error_count": r.error_count,
                    "avg_confidence": round(float(r.avg_confidence or 0), 3),
                    "last_review_at": r.last_review_at.isoformat() if r.last_review_at else "",
                    "entities": [_row_to_dict(er) for er in entity_rows],
                })
            return (docs, total)
    except Exception as e:
        logger.error(f"[agent_run_repo] 文档维度查询失败: {e}")
        return ([], 0)


def get_runs_for_document(document_uuid: str) -> list[dict]:
    """查询指定文档下所有实体/关系审核运行记录（含完整 steps）。"""
    try:
        with get_session() as session:
            rows = session.execute(
                text("""
                    SELECT * FROM agent_run
                    WHERE agent_type IN ('entity_review', 'relation_review')
                      AND document_uuid = :doc_uuid
                    ORDER BY created_at DESC
                """),
                {"doc_uuid": document_uuid},
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[agent_run_repo] 文档查询失败: {e}")
        return []


def count_pending_entities(document_uuid: str) -> int:
    """获取指定文档下 pending 状态的实体数量。"""
    try:
        with get_session() as session:
            from db.models import ExtractedEntity
            count = (
                session.query(ExtractedEntity.id)
                .filter(
                    ExtractedEntity.source_doc_uuid == document_uuid,
                    ExtractedEntity.review_status == "pending",
                    ExtractedEntity.is_deleted == False,
                )
                .count()
            )
            return count
    except Exception as e:
        logger.error(f"[agent_run_repo] pending 计数失败: {e}")
        return 0


def get_agent_stats() -> dict:
    """Agent 运行统计。"""
    try:
        with get_session() as session:
            rows = session.execute(
                text("""
                    SELECT agent_type, decision, COUNT(*) as cnt
                    FROM agent_run
                    WHERE status = 'completed'
                    GROUP BY agent_type, decision
                """)
            ).fetchall()
            stats = {}
            for agent_type, decision, cnt in rows:
                if agent_type not in stats:
                    stats[agent_type] = {}
                stats[agent_type][decision] = cnt
            return stats
    except Exception as e:
        logger.error(f"[agent_run_repo] 统计失败: {e}")
        return {}


# 内存级「审核中」追踪：后端异步任务启动时加入，完成时移除。页面刷新不丢失。
_active_reviews: set[str] = set()


def mark_document_reviewing(document_uuid: str) -> None:
    _active_reviews.add(document_uuid)


def unmark_document_reviewing(document_uuid: str) -> None:
    _active_reviews.discard(document_uuid)


def is_document_reviewing(document_uuid: str) -> bool:
    return document_uuid in _active_reviews


def list_documents_with_entities(
    page: int = 1,
    page_size: int = 20,
    status: str = "",
) -> tuple[list[dict], int]:
    """分页列出有实体的文档，含审核状态。status 可选: pending / reviewed。返回 (items, total)。"""
    try:
        with get_session() as session:
            base_sql = """
                SELECT
                    d.uuid, d.title, d.source_site, d.doc_type, d.created_at,
                    d.pending_entity_count, d.total_entity_count,
                    COALESCE((
                        SELECT COUNT(*) FROM entity_relations
                        WHERE source_doc_uuid = d.uuid
                          AND review_status = 'pending'
                          AND is_deleted = false
                    ), 0) AS pending_relation_count,
                    COALESCE((
                        SELECT COUNT(*) FROM entity_relations
                        WHERE source_doc_uuid = d.uuid
                          AND is_deleted = false
                    ), 0) AS total_relation_count
                FROM documents d
                WHERE d.is_deleted = false
                  AND d.total_entity_count > 0
            """
            order_sql = """
                ORDER BY
                    CASE WHEN d.pending_entity_count > 0 THEN 0 ELSE 1 END,
                    d.pending_entity_count DESC
            """

            where_extra = ""
            if status == "pending":
                where_extra = "AND d.pending_entity_count > 0"
            elif status == "reviewed":
                where_extra = "AND d.pending_entity_count = 0"

            total = session.execute(
                text(f"SELECT COUNT(*) FROM ({base_sql} {where_extra}) AS _count")
            ).scalar() or 0

            offset = (page - 1) * page_size
            rows = session.execute(
                text(f"{base_sql} {where_extra} {order_sql} OFFSET :offset LIMIT :limit"),
                {"offset": offset, "limit": page_size},
            ).fetchall()

            items = [
                {
                    "document_uuid": r.uuid,
                    "document_title": r.title or "",
                    "source_site": r.source_site or "",
                    "doc_type": r.doc_type or "",
                    "pending_count": r.pending_entity_count,
                    "total_entity_count": r.total_entity_count,
                    "pending_relation_count": r.pending_relation_count,
                    "total_relation_count": r.total_relation_count,
                    "review_status": _compute_doc_review_status(
                        r.pending_entity_count, r.total_entity_count, r.uuid
                    ),
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]
            return (items, total)
    except Exception as e:
        logger.error(f"[agent_run_repo] 文档列表查询失败: {e}")
        return ([], 0)


def _compute_doc_review_status(pending: int, total: int, doc_uuid: str = "") -> str:
    if doc_uuid and doc_uuid in _active_reviews:
        return "reviewing"
    if total == 0 or pending > 0:
        return "pending"
    return "reviewed"


def _row_to_dict(row) -> dict:
    """将 SQLAlchemy Row 转为 dict。"""
    d = dict(row._mapping)
    for key in ("steps",):
        val = d.get(key)
        if isinstance(val, str):
            try:
                d[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    for key in ("started_at", "completed_at", "created_at"):
        val = d.get(key)
        if isinstance(val, datetime):
            d[key] = val.isoformat()
    return d
