"""WeKnora synchronisation flags and graph push bookkeeping for dictionary rows, entities, relations."""

from loguru import logger
from sqlalchemy import false, true

from graph.db.models import (
    EntityAlias,
    EntityDict,
    EntityRelation,
    ExtractedEntity,
)
from graph.db.repository.audit import _write_operation_log


def get_entity_dict_for_weknora(limit: int = 500, offset: int = 0) -> list[dict]:
    """
    查询 weknora_sync_status='pending' 且 is_deleted=False 且 is_verified=True 的 entity_dict 行。
    未审核实体不推送 WeKnora，避免前端出现不完整数据。
    返回字段：id, entity_type, external_ids, canonical_data, canonical_source, is_deleted
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        q = (
            session.query(EntityDict)
            .filter(
                EntityDict.weknora_sync_status == "pending",
                EntityDict.is_deleted == false(),
                EntityDict.is_verified == true(),
            )
        )
        if limit:
            q = q.limit(limit)
        if offset:
            q = q.offset(offset)
        rows = q.all()
        return [
            {
                "id": r.id,
                "entity_type": r.entity_type,
                "external_ids": r.external_ids or {},
                "canonical_data": r.canonical_data or {},
                "canonical_source": r.canonical_source or "",
                "is_deleted": r.is_deleted,
            }
            for r in rows
        ]


def mark_entity_dict_synced(dict_id: int) -> None:
    """标记 entity_dict 行已同步到 WeKnora。"""
    batch_mark_entity_dict_synced([dict_id])


def mark_entity_dict_failed(dict_id: int, error: str = "") -> None:
    """标记 entity_dict 行同步失败。"""
    batch_mark_entity_dict_failed([dict_id], error)


def batch_mark_entity_dict_synced(ids: list[int]) -> None:
    """批量标记 entity_dict 行已同步到 WeKnora（单条 UPDATE）。"""
    if not ids:
        return
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        session.query(EntityDict).filter(
            EntityDict.id.in_(ids)
        ).update({"weknora_sync_status": "synced"}, synchronize_session=False)
        _write_operation_log(
            session, "update", "entity_dict", None,
            business_action="weknora_sync",
            description=f"批量标记已同步: {len(ids)} 条 entity_dict",
            details={"ids": ids, "weknora_sync_status": "synced"},
            trigger_source="batch_mark_entity_dict_synced",
        )
        logger.info(f"[DB] 批量标记 entity_dict synced: {len(ids)} 条")


def batch_mark_entity_dict_failed(ids: list[int], error: str = "") -> None:
    """批量标记 entity_dict 行同步失败（单条 UPDATE）。"""
    if not ids:
        return
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        session.query(EntityDict).filter(
            EntityDict.id.in_(ids)
        ).update({"weknora_sync_status": "failed"}, synchronize_session=False)
        _write_operation_log(
            session, "update", "entity_dict", None,
            business_action="weknora_sync",
            description=f"批量标记同步失败: {len(ids)} 条 entity_dict",
            details={"ids": ids, "weknora_sync_status": "failed", "error": error},
            trigger_source="batch_mark_entity_dict_failed",
        )
        if error:
            logger.warning(f"[DB] 批量标记 entity_dict failed: {len(ids)} 条, error={error[:200]}")
        else:
            logger.warning(f"[DB] 批量标记 entity_dict failed: {len(ids)} 条")


def get_entities_for_weknora_graph(limit: int = 500, offset: int = 0) -> list[dict]:
    """
    查询审核通过且 weknora_graph_status='pending' 的实体。
    返回字段：id, entity_type, entity_name, entity_data, source_site,
             confidence_score, review_status, is_deleted
    entity_data 中额外注入：
      - name: 与 entity_name 相同
      - name_aliases: 从 entity_aliases 查到的别名列表
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        query = (
            session.query(ExtractedEntity)
            .filter(
                ExtractedEntity.review_status.in_(["approved", "auto_approved"]),
                ExtractedEntity.weknora_graph_status == "pending",
                ExtractedEntity.is_deleted == false(),
            )
        )
        if limit > 0:
            query = query.limit(limit)
        if offset > 0:
            query = query.offset(offset)
        rows = query.all()

        # 批量查 aliases
        dict_ids = {r.dict_id for r in rows if r.dict_id}
        alias_map: dict[int, list[str]] = {}
        if dict_ids:
            alias_rows = (
                session.query(EntityAlias)
                .filter(
                    EntityAlias.dict_id.in_(dict_ids),
                    EntityAlias.is_deleted == false(),
                )
                .all()
            )
            for a in alias_rows:
                alias_map.setdefault(a.dict_id, []).append(a.name)

        result = []
        for r in rows:
            entity_data = dict(r.entity_data or {})
            entity_data["name"] = r.entity_name or ""
            entity_data["name_aliases"] = alias_map.get(r.dict_id, [])
            result.append({
                "id": r.id,
                "dict_id": r.dict_id,
                "entity_type": r.entity_type,
                "entity_name": r.entity_name,
                "entity_data": entity_data,
                "source_site": r.source_site or "",
                "confidence_score": r.confidence_score or 0.0,
                "review_status": r.review_status,
                "is_deleted": r.is_deleted,
            })
        return result


def mark_entity_weknora_graph_pushed(entity_id: int) -> None:
    """标记实体已推送到 WeKnora graph_entities。"""
    batch_mark_entity_weknora_graph_pushed([entity_id])


def mark_entity_weknora_graph_failed(entity_id: int, error: str = "") -> None:
    """标记实体推送 WeKnora graph_entities 失败。"""
    batch_mark_entity_weknora_graph_failed([entity_id], error)


def batch_mark_entity_weknora_graph_pushed(entity_ids: list[int]) -> None:
    """批量标记实体已推送到 WeKnora graph_entities（单条 UPDATE）。"""
    if not entity_ids:
        return
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        session.query(ExtractedEntity).filter(
            ExtractedEntity.id.in_(entity_ids)
        ).update({"weknora_graph_status": "pushed"}, synchronize_session=False)
        _write_operation_log(
            session, "update", "extracted_entities", None,
            business_action="graph_sync",
            description=f"批量标记实体图同步完成: {len(entity_ids)} 条",
            details={"entity_ids": entity_ids, "weknora_graph_status": "pushed"},
            trigger_source="batch_mark_entity_weknora_graph_pushed",
        )
        logger.info(f"[DB] 批量标记实体 graph pushed: {len(entity_ids)} 条")


def batch_mark_entity_weknora_graph_failed(entity_ids: list[int], error: str = "") -> None:
    """批量标记实体推送 WeKnora graph_entities 失败（单条 UPDATE）。"""
    if not entity_ids:
        return
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        session.query(ExtractedEntity).filter(
            ExtractedEntity.id.in_(entity_ids)
        ).update({"weknora_graph_status": "failed"}, synchronize_session=False)
        _write_operation_log(
            session, "update", "extracted_entities", None,
            business_action="graph_sync",
            description=f"批量标记实体图同步失败: {len(entity_ids)} 条",
            details={"entity_ids": entity_ids, "weknora_graph_status": "failed", "error": error},
            trigger_source="batch_mark_entity_weknora_graph_failed",
        )
        if error:
            logger.warning(f"[DB] 批量标记实体 graph failed: {len(entity_ids)} 条, error={error[:200]}")
        else:
            logger.warning(f"[DB] 批量标记实体 graph failed: {len(entity_ids)} 条")


def get_relations_for_weknora_graph(limit: int = 500, offset: int = 0) -> list[dict]:
    """
    查询审核通过且 weknora_graph_status='pending' 的关系。
    JOIN 两端实体以获取 dict_id。
    返回字段：id, from_dict_id, to_dict_id, relation_type, relation_props, is_deleted
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        from sqlalchemy.orm import aliased
        FromEntity = aliased(ExtractedEntity)
        ToEntity = aliased(ExtractedEntity)
        query = (
            session.query(
                EntityRelation.id,
                EntityRelation.relation_type,
                EntityRelation.relation_props,
                EntityRelation.is_deleted,
                FromEntity.dict_id.label("from_dict_id"),
                ToEntity.dict_id.label("to_dict_id"),
            )
            .join(FromEntity, EntityRelation.from_entity_id == FromEntity.id)
            .join(ToEntity, EntityRelation.to_entity_id == ToEntity.id)
            .filter(
                EntityRelation.review_status.in_(["approved", "auto_approved"]),
                EntityRelation.weknora_graph_status == "pending",
                EntityRelation.is_deleted == false(),
                FromEntity.is_deleted == false(),
                ToEntity.is_deleted == false(),
            )
        )
        if limit > 0:
            query = query.limit(limit)
        if offset > 0:
            query = query.offset(offset)
        rows = query.all()
        return [
            {
                "id": r.id,
                "from_dict_id": r.from_dict_id,
                "to_dict_id": r.to_dict_id,
                "relation_type": r.relation_type,
                "relation_props": r.relation_props or {},
                "is_deleted": r.is_deleted,
            }
            for r in rows
        ]


def mark_relation_weknora_graph_pushed(relation_id: int) -> None:
    """标记关系已推送到 WeKnora graph_relations。"""
    batch_mark_relation_weknora_graph_pushed([relation_id])


def mark_relation_weknora_graph_failed(relation_id: int, error: str = "") -> None:
    """标记关系推送 WeKnora graph_relations 失败。"""
    batch_mark_relation_weknora_graph_failed([relation_id], error)


def batch_mark_relation_weknora_graph_pushed(relation_ids: list[int]) -> None:
    """批量标记关系已推送到 WeKnora graph_relations（单条 UPDATE）。"""
    if not relation_ids:
        return
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        session.query(EntityRelation).filter(
            EntityRelation.id.in_(relation_ids)
        ).update({"weknora_graph_status": "pushed"}, synchronize_session=False)
        _write_operation_log(
            session, "update", "entity_relations", None,
            business_action="graph_sync",
            description=f"批量标记关系图同步完成: {len(relation_ids)} 条",
            details={"relation_ids": relation_ids, "weknora_graph_status": "pushed"},
            trigger_source="batch_mark_relation_weknora_graph_pushed",
        )
        logger.info(f"[DB] 批量标记关系 graph pushed: {len(relation_ids)} 条")


def batch_mark_relation_weknora_graph_failed(relation_ids: list[int], error: str = "") -> None:
    """批量标记关系推送 WeKnora graph_relations 失败（单条 UPDATE）。"""
    if not relation_ids:
        return
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        session.query(EntityRelation).filter(
            EntityRelation.id.in_(relation_ids)
        ).update({"weknora_graph_status": "failed"}, synchronize_session=False)
        _write_operation_log(
            session, "update", "entity_relations", None,
            business_action="graph_sync",
            description=f"批量标记关系图同步失败: {len(relation_ids)} 条",
            details={"relation_ids": relation_ids, "weknora_graph_status": "failed", "error": error},
            trigger_source="batch_mark_relation_weknora_graph_failed",
        )
        if error:
            logger.warning(f"[DB] 批量标记关系 graph failed: {len(relation_ids)} 条, error={error[:200]}")
        else:
            logger.warning(f"[DB] 批量标记关系 graph failed: {len(relation_ids)} 条")


def get_weknora_graph_stats() -> dict:
    """获取 WeKnora 图同步状态统计。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return {
                "entity_dict": {"pending": 0, "synced": 0, "failed": 0},
                "entities": {"pending": 0, "pushed": 0, "failed": 0},
                "relations": {"pending": 0, "pushed": 0, "failed": 0},
            }

        def _count(model, field: str, val: str) -> int:
            return session.query(model).filter(
                getattr(model, field) == val,
                model.is_deleted == false(),
            ).count()

        return {
            "entity_dict": {
                "pending": _count(EntityDict, "weknora_sync_status", "pending"),
                "synced": _count(EntityDict, "weknora_sync_status", "synced"),
                "failed": _count(EntityDict, "weknora_sync_status", "failed"),
            },
            "entities": {
                "pending": _count(ExtractedEntity, "weknora_graph_status", "pending"),
                "pushed": _count(ExtractedEntity, "weknora_graph_status", "pushed"),
                "failed": _count(ExtractedEntity, "weknora_graph_status", "failed"),
            },
            "relations": {
                "pending": _count(EntityRelation, "weknora_graph_status", "pending"),
                "pushed": _count(EntityRelation, "weknora_graph_status", "pushed"),
                "failed": _count(EntityRelation, "weknora_graph_status", "failed"),
            },
        }
