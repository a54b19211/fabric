"""Entity merge queue, identity merge proposals, merge/rollback and merge logs."""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import false, func

from graph.db.models import (
    EntityAlias,
    EntityDict,
    EntityIdentityMergeProposal,
    EntityMergeLog,
    EntityMergeQueue,
    ExtractedEntity,
)
from graph.db.repository.audit import _write_operation_log
from graph.db.repository.canonical import _is_empty_value, _sync_canonical_to_extracted


def create_identity_merge_proposal(source_entity_id, target_dict_id, agent_reason='', agent_confidence=0.0,
                                   agent_run_id='', session=None, **metadata):
    from graph.db.identity import add_identity_proposal, use_session
    with use_session(session) as active:
        return add_identity_proposal(active, source_entity_id, target_dict_id, agent_reason,
                                     agent_confidence, agent_run_id, **metadata)


def get_identity_merge_proposals(status: str = "pending", limit: int = 100) -> list[dict]:
    """查询身份合并提案列表。

    Args:
        status: pending | approved | rejected，默认 pending
        limit: 返回数量上限

    Returns:
        提案列表，每项包含双方快照和 Agent 决策信息
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []

        rows = (
            session.query(EntityIdentityMergeProposal)
            .filter(EntityIdentityMergeProposal.status == status)
            .order_by(EntityIdentityMergeProposal.created_at.desc())
            .limit(limit)
            .all()
        )

        return [
            {
                "id": r.id,
                "source_entity_id": r.source_entity_id,
                "target_dict_id": r.target_dict_id,
                "source_entity_snapshot": r.source_entity_snapshot or {},
                "target_dict_snapshot": r.target_dict_snapshot or {},
                "agent_run_id": r.agent_run_id,
                "agent_reason": r.agent_reason,
                "agent_confidence": r.agent_confidence,
                "match_step": r.match_step,
                "rule_version": r.rule_version,
                "model_version": r.model_version,
                "candidate_payload": r.candidate_payload or {},
                "evidence_key": r.evidence_key,
                "merge_log_id": r.merge_log_id,
                "status": r.status,
                "reviewer": r.reviewer,
                "review_note": r.review_note,
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else "",
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ]


def resolve_identity_merge_proposal(proposal_id, status, reviewer='', note='', session=None, review_context=None):
    """Apply or reject the link and record its decision in one source transaction."""
    from graph.db.identity import resolve_link_proposal
    return resolve_link_proposal(proposal_id, status, reviewer, note, session=session, review_context=review_context)


def _unwrap_val(value):
    """Queue values are stored as {"_val": x} when x is not a dict/list."""
    return value.get("_val") if isinstance(value, dict) and list(value.keys()) == ["_val"] else value


def _log_auto_resolve(session, r, description, details):
    _write_operation_log(
        session, "update", "entity_merge_queue", r.id,
        business_action="auto_resolve", description=description, details=details,
        trigger_source="get_pending_merges",
    )


def _auto_resolve_stale_conflict(session, r, entity, source_ent) -> bool:
    """Close a queue row that no longer needs a human decision. True when it was resolved here."""
    if entity is None:
        r.resolution = "resolved_dict_deleted"
        r.reviewed_at = datetime.now(timezone.utc)
        _log_auto_resolve(session, r, f"dict 已删除: dict_id={r.target_dict_id} field={r.field_name}",
                          {"reason": "dict_deleted", "dict_id": r.target_dict_id})
        return True
    if source_ent is None:
        r.resolution = "resolved_source_deleted"
        r.reviewed_at = datetime.now(timezone.utc)
        _log_auto_resolve(session, r, f"source 实体已删除: entity_id={r.source_entity_id} field={r.field_name}",
                          {"reason": "source_deleted", "source_entity_id": r.source_entity_id})
        return True
    # 值相等 / 一侧语义为空 → 自动 resolve
    current_val = (entity.canonical_data or {}).get(r.field_name)
    new_val = _unwrap_val(r.new_value)
    cur_empty = _is_empty_value(current_val)
    new_empty = _is_empty_value(new_val)
    if cur_empty and new_empty:
        r.resolution = "resolved_both_empty"
        r.reviewed_at = datetime.now(timezone.utc)
        return True
    if cur_empty and not new_empty:
        r.resolution = "accept_new"
        r.reviewed_at = datetime.now(timezone.utc)
        cd = dict(entity.canonical_data or {})
        cd[r.field_name] = r.new_value
        from graph.db.identity import set_dictionary_identity_data
        set_dictionary_identity_data(session, entity, canonical_data=cd)
        entity.canonical_updated_at = datetime.now(timezone.utc)
        return True
    if new_empty and not cur_empty:
        r.resolution = "keep_existing"
        r.reviewed_at = datetime.now(timezone.utc)
        return True
    if current_val == new_val:
        r.resolution = "resolved_already_merged"
        r.reviewed_at = datetime.now(timezone.utc)
        _log_auto_resolve(session, r, f"值已通过其他途径合并: dict_id={r.target_dict_id} field={r.field_name}",
                          {"reason": "already_merged", "current_value": current_val})
        return True
    return False


def _conflict_group(r, entity) -> dict:
    cd = entity.canonical_data or {}
    return {
        "dict_id": r.target_dict_id,
        "entity_type": entity.entity_type,
        "entity_name": cd.get("name", ""),
        "canonical_data": cd,
        "canonical_source": entity.canonical_source or "",
        "conflicts": [],
    }


def _conflict_entry(r, source_ent) -> dict:
    return {
        "merge_id": r.id,
        "field_name": r.field_name,
        "existing_value": _unwrap_val(r.existing_value),
        "new_value": _unwrap_val(r.new_value),
        "source_entity_id": r.source_entity_id,
        "source_site": r.source_site,
        "source_doc_uuid": source_ent.source_doc_uuid,
        "existing_source_doc_uuid": r.existing_source_doc_uuid or "",
    }


def _group_pending_conflicts(session, rows, dict_map, source_ent_map):
    """Freshness-check each queue row; return the open conflicts grouped by dictionary and the auto-resolved count."""
    groups: dict[int, dict] = {}
    auto_resolved = 0
    for r in rows:
        entity = dict_map.get(r.target_dict_id)
        source_ent = source_ent_map.get(r.source_entity_id)
        if _auto_resolve_stale_conflict(session, r, entity, source_ent):
            auto_resolved += 1
            continue
        # 刷新过期的 existing_value
        current_val = (entity.canonical_data or {}).get(r.field_name)
        existing_val = _unwrap_val(r.existing_value)
        if existing_val != current_val and current_val is not None:
            r.existing_value = current_val if isinstance(current_val, (dict, list)) else {"_val": current_val}
        if r.target_dict_id not in groups:
            groups[r.target_dict_id] = _conflict_group(r, entity)
        groups[r.target_dict_id]["conflicts"].append(_conflict_entry(r, source_ent))
    return groups, auto_resolved


def _attach_conflict_sources(session, groups) -> None:
    """补全来源文档列表：查出所有关联实体，按字段值分组"""
    group_dict_ids = list(groups.keys())
    if not group_dict_ids:
        return
    all_related_entities = session.query(ExtractedEntity).filter(ExtractedEntity.dict_id.in_(group_dict_ids)).all()
    from collections import defaultdict
    entities_by_dict: dict[int, list] = defaultdict(list)
    for ent in all_related_entities:
        entities_by_dict[ent.dict_id].append(ent)
    for g in groups.values():
        dict_entities = entities_by_dict.get(g["dict_id"], [])
        for c in g["conflicts"]:
            ev = c["existing_value"]
            nv = c["new_value"]
            existing_sources: list[dict] = []
            new_sources: list[dict] = []
            seen_existing: set[str] = set()
            seen_new: set[str] = set()
            for ent in dict_entities:
                ent_val = (ent.entity_data or {}).get(c["field_name"])
                if ent_val is None:
                    continue
                doc = ent.source_doc_uuid or ""
                src = {"entity_id": ent.id, "source_doc_uuid": doc, "source_site": ent.source_site or ""}
                if ent_val == ev and doc not in seen_existing:
                    existing_sources.append(src)
                    seen_existing.add(doc)
                elif ent_val == nv and doc not in seen_new:
                    new_sources.append(src)
                    seen_new.add(doc)
            c["existing_sources"] = existing_sources
            c["new_sources"] = new_sources


def get_pending_merges(limit: int = 50) -> dict:
    """
    查询待处理合并冲突，按 dict_id 聚合。
    返回前校验新鲜度（批量查表，无 N+1），自动 resolve 已过期冲突。
    """
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return {"total_groups": 0, "groups": []}

        # 1. 查待处理 dict_id（limit 在 dict_id 层面）
        dict_id_rows = (
            session.query(EntityMergeQueue.target_dict_id)
            .filter(EntityMergeQueue.resolution == "pending")
            .distinct()
            .order_by(EntityMergeQueue.target_dict_id.desc())
            .limit(limit)
            .all()
        )
        if not dict_id_rows:
            return {"total_groups": 0, "groups": []}

        dict_ids = [r[0] for r in dict_id_rows]

        # 2. 拉取这些 dict_id 的所有 pending 冲突
        rows = (
            session.query(EntityMergeQueue)
            .filter(
                EntityMergeQueue.resolution == "pending",
                EntityMergeQueue.target_dict_id.in_(dict_ids),
            )
            .order_by(EntityMergeQueue.created_at.desc())
            .all()
        )

        # 3. 批量预加载关联数据，避免 N+1
        entity_ids = list({r.target_dict_id for r in rows})
        source_ent_ids = list({r.source_entity_id for r in rows})

        dict_map = {
            e.id: e
            for e in session.query(EntityDict).filter(
                EntityDict.id.in_(entity_ids),
                EntityDict.is_deleted == false(),
            ).all()
        }
        source_ent_map = {
            se.id: se
            for se in session.query(ExtractedEntity).filter(
                ExtractedEntity.id.in_(source_ent_ids),
                ExtractedEntity.is_deleted == false(),
            ).all()
        }

        # 4. 逐条校验新鲜度
        groups, auto_resolved = _group_pending_conflicts(session, rows, dict_map, source_ent_map)
        if auto_resolved:
            session.commit()
            logger.info(f"[DB] get_pending_merges: auto resolved {auto_resolved} 条过期冲突")

        # 5. 补全来源文档列表
        _attach_conflict_sources(session, groups)

        sorted_groups = sorted(groups.values(), key=lambda g: g["dict_id"], reverse=True)
        return {"total_groups": len(sorted_groups), "groups": sorted_groups}


def resolve_merge(
    merge_id: int,
    resolution: str,
    reviewer: str = "",
    resolved_value: dict | None = None,
    session=None, review_context=None,
) -> None:
    """
    解决合并冲突。
    resolution: accept_new | keep_existing | custom
    custom 时需要传 resolved_value。
    """
    from api.review_auth import require_trusted_review_context
    from graph.db.identity import use_session
    reviewer = require_trusted_review_context(review_context)
    if resolution not in ('accept_new', 'keep_existing', 'custom') or (resolution == 'custom' and resolved_value is None):
        raise ValueError('Invalid field resolution')

    with use_session(session) as session:
        if session is None:
            return

        merge = session.query(EntityMergeQueue).filter(EntityMergeQueue.id == merge_id, EntityMergeQueue.resolution == 'pending').populate_existing().with_for_update().first()
        if merge is None:
            logger.warning(f"[DB] resolve_merge: merge_id={merge_id} 不存在")
            return

        merge.resolution = resolution
        merge.reviewed_by = reviewer
        merge.reviewed_at = datetime.now(timezone.utc)

        if resolution == "accept_new":
            entity = session.query(EntityDict).filter(EntityDict.id == merge.target_dict_id).first()
            if entity:
                canonical = dict(entity.canonical_data or {})
                canonical[merge.field_name] = merge.new_value.get('_val') if isinstance(merge.new_value, dict) and list(merge.new_value) == ['_val'] else merge.new_value
                from graph.db.identity import set_dictionary_identity_data
                set_dictionary_identity_data(session, entity, canonical_data=canonical)
                entity.canonical_updated_at = datetime.now(timezone.utc)
        elif resolution == "custom" and resolved_value is not None:
            merge.resolved_value = resolved_value
            entity = session.query(EntityDict).filter(EntityDict.id == merge.target_dict_id).first()
            if entity:
                canonical = dict(entity.canonical_data or {})
                canonical[merge.field_name] = resolved_value
                from graph.db.identity import set_dictionary_identity_data
                set_dictionary_identity_data(session, entity, canonical_data=canonical)
                entity.canonical_updated_at = datetime.now(timezone.utc)

        _write_operation_log(
            session, "update", "entity_merge_queue", merge_id,
            business_action="merge_resolve",
            description=f"合并冲突解决: field={merge.field_name} resolution={resolution} by {reviewer}",
            details={"field_name": merge.field_name, "resolution": resolution,
                      "target_dict_id": merge.target_dict_id, "reviewer": reviewer},
            operator=reviewer,
            trigger_source="resolve_merge",
        )

        # 回写 canonical_data → entity_data
        _sync_canonical_to_extracted(session, merge.target_dict_id)

        logger.info(f"[DB] resolve_merge: merge_id={merge_id} resolution={resolution}")


def batch_resolve_merges(
    decisions: list[dict],
    reviewer: str = "",
    session=None, review_context=None,
) -> dict:
    """
    批量解决合并冲突。一次请求解决同 dict_id 下所有冲突字段。
    decisions: [{merge_id, resolution, resolved_value?}]
    """
    from api.review_auth import require_trusted_review_context
    from graph.db.identity import use_session
    reviewer = require_trusted_review_context(review_context)
    for decision in decisions:
        if decision.get('resolution') not in ('accept_new', 'keep_existing', 'custom') or (decision.get('resolution') == 'custom' and decision.get('resolved_value') is None):
            raise ValueError('Invalid field resolution')

    with use_session(session) as session:
        if session is None:
            return {"success": False, "error": "数据库未连接"}

        resolved_count = 0
        affected_dict_ids = set()
        for dec in decisions:
            merge_id = dec["merge_id"]
            resolution = dec["resolution"]
            resolved_value = dec.get("resolved_value")

            merge = session.query(EntityMergeQueue).filter(
                EntityMergeQueue.id == merge_id,
                EntityMergeQueue.resolution == "pending",
            ).populate_existing().with_for_update().first()
            if merge is None:
                continue

            merge.resolution = resolution
            merge.reviewed_by = reviewer
            merge.reviewed_at = datetime.now(timezone.utc)

            if resolution == "accept_new":
                entity = session.query(EntityDict).filter(
                    EntityDict.id == merge.target_dict_id,
                    EntityDict.is_deleted == false(),
                ).first()
                if entity:
                    canonical = dict(entity.canonical_data or {})
                    new_val = merge.new_value.get("_val") if isinstance(merge.new_value, dict) and list(merge.new_value.keys()) == ["_val"] else merge.new_value
                    canonical[merge.field_name] = new_val
                    from graph.db.identity import set_dictionary_identity_data
                    set_dictionary_identity_data(session, entity, canonical_data=canonical)
                    entity.canonical_updated_at = datetime.now(timezone.utc)
                    affected_dict_ids.add(merge.target_dict_id)
            elif resolution == "keep_existing":
                pass
            elif resolution == "custom" and resolved_value is not None:
                merge.resolved_value = resolved_value
                entity = session.query(EntityDict).filter(
                    EntityDict.id == merge.target_dict_id,
                    EntityDict.is_deleted == false(),
                ).first()
                if entity:
                    canonical = dict(entity.canonical_data or {})
                    canonical[merge.field_name] = resolved_value
                    from graph.db.identity import set_dictionary_identity_data
                    set_dictionary_identity_data(session, entity, canonical_data=canonical)
                    entity.canonical_updated_at = datetime.now(timezone.utc)
                    affected_dict_ids.add(merge.target_dict_id)

            resolved_count += 1
            _write_operation_log(
                session, "update", "entity_merge_queue", merge_id,
                business_action="merge_resolve",
                description=f"批量合并冲突解决: field={merge.field_name} resolution={resolution}",
                details={"field_name": merge.field_name, "resolution": resolution,
                          "target_dict_id": merge.target_dict_id, "reviewer": reviewer},
                operator=reviewer,
                trigger_source="batch_resolve_merges",
            )

        for _dict_id in affected_dict_ids:
            _sync_canonical_to_extracted(session, _dict_id)

        session.flush()
        logger.info(f"[DB] batch_resolve_merges: resolved {resolved_count} conflicts")

        return {"success": True, "resolved_count": resolved_count}


def search_entity_dicts(q: str, limit: int = 20) -> list[dict]:
    """模糊搜索 entity_dict：按名称或别名匹配。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []

        pattern = f"%{q.strip()}%"
        results: list[dict] = []
        seen: set[int] = set()

        # 1. 按别名搜索
        alias_rows = (
            session.query(EntityAlias.dict_id, EntityAlias.name)
            .filter(
                EntityAlias.is_deleted == false(),
                func.lower(EntityAlias.name).like(func.lower(pattern)),
            )
            .limit(limit)
            .all()
        )
        for dict_id, matched_alias in alias_rows:
            if dict_id in seen:
                continue
            entity = session.query(EntityDict).filter(
                EntityDict.id == dict_id,
                EntityDict.is_deleted == false(),
            ).first()
            if entity is None:
                continue
            seen.add(dict_id)
            results.append(_build_search_result(session, entity))

        # 2. 按 canonical_data.name 搜索
        remaining = limit - len(results)
        if remaining > 0:
            name_rows = (
                session.query(EntityDict)
                .filter(
                    EntityDict.is_deleted == false(),
                    func.lower(func.jsonb_extract_path_text(
                        EntityDict.canonical_data, "name"
                    )).like(func.lower(pattern)),
                )
                .limit(remaining)
                .all()
            )
            for entity in name_rows:
                if entity.id in seen:
                    continue
                seen.add(entity.id)
                results.append(_build_search_result(session, entity))

        return results


def _build_search_result(session, entity: EntityDict) -> dict:
    """构建搜索结果的 dict。"""
    aliases = [
        a.name for a in session.query(EntityAlias.name).filter(
            EntityAlias.dict_id == entity.id,
            EntityAlias.is_deleted == false(),
        ).all()
    ]
    count = session.query(ExtractedEntity).filter(
        ExtractedEntity.dict_id == entity.id,
        ExtractedEntity.is_deleted == false(),
    ).count()

    cd = entity.canonical_data or {}
    return {
        "dict_id": entity.id,
        "entity_type": entity.entity_type,
        "entity_name": cd.get("name", ""),
        "canonical_source": entity.canonical_source or "",
        "aliases": aliases,
        "extracted_entities_count": count,
    }


def compare_entities(dict_ids: list[int]) -> dict:
    """对比多个 entity_dict 的 canonical_data，返回 entity 信息和 field_diff。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return {"entities": [], "field_diff": []}

        entities_raw = (
            session.query(EntityDict)
            .filter(EntityDict.id.in_(dict_ids), EntityDict.is_deleted == false())
            .all()
        )
        if not entities_raw:
            return {"entities": [], "field_diff": []}

        entities_info: list[dict] = []
        all_fields: set[str] = set()
        values_by_field: dict[str, dict[str, object]] = {}

        for ent in entities_raw:
            cd = ent.canonical_data or {}
            all_fields.update(cd.keys())

            aliases = [
                a.name for a in session.query(EntityAlias.name).filter(
                    EntityAlias.dict_id == ent.id,
                    EntityAlias.is_deleted == false(),
                ).all()
            ]
            count = session.query(ExtractedEntity).filter(
                ExtractedEntity.dict_id == ent.id,
                ExtractedEntity.is_deleted == false(),
            ).count()

            entities_info.append({
                "dict_id": ent.id,
                "entity_type": ent.entity_type,
                "entity_name": cd.get("name", ""),
                "canonical_data": cd,
                "canonical_source": ent.canonical_source or "",
                "aliases": aliases,
                "extracted_entities_count": count,
            })

            for key, val in cd.items():
                if key not in values_by_field:
                    values_by_field[key] = {}
                values_by_field[key][str(ent.id)] = val

    # 跳过 _SKIP_MERGE_FIELDS 和内部字段
    skip_fields = {"name", "name_en", "aliases", "source_text"}

    field_diff: list[dict] = []
    for field in sorted(all_fields):
        if field.startswith("_") or field in skip_fields:
            continue
        vals = values_by_field.get(field, {})
        unique = set(
            tuple(v) if isinstance(v, list) else str(v)
            for v in vals.values()
        )
        conflict = len(unique) > 1
        field_diff.append({
            "field": field,
            "values": vals,
            "conflict": conflict,
        })

    return {"entities": entities_info, "field_diff": field_diff}


def merge_entities(keeper_dict_id, absorbed_dict_ids, field_decisions, reviewer='', note='', session=None, review_context=None):
    from graph.db.identity import merge_dictionary_identities, use_session
    with use_session(session) as active:
        return merge_dictionary_identities(active, keeper_dict_id, absorbed_dict_ids, field_decisions, reviewer, note, review_context=review_context)


def rollback_merge(merge_log_id, reason='', session=None, review_context=None):
    from api.review_auth import require_trusted_review_context
    from graph.db.identity import rollback_identity_merge, use_session
    require_trusted_review_context(review_context)
    with use_session(session) as active:
        return rollback_identity_merge(active, merge_log_id, reason, review_context=review_context)


def get_merge_logs(limit: int = 50, include_rolled_back: bool = True, dict_ids=None, session=None) -> list[dict]:
    """查询合并日志列表。"""
    from sqlalchemy import or_

    from graph.db.identity import use_session

    with use_session(session) as session:

        q = session.query(EntityMergeLog).order_by(EntityMergeLog.created_at.desc())
        if dict_ids is not None:
            selected_ids = sorted({int(value) for value in dict_ids})
            if not selected_ids:
                return []
            q = q.filter(or_(EntityMergeLog.keeper_dict_id.in_(selected_ids),
                             EntityMergeLog.absorbed_dict_ids.overlap(selected_ids)))
        if not include_rolled_back:
            q = q.filter(EntityMergeLog.is_rolled_back == false())
        rows = q.limit(limit).all()

        return [
            {
                "id": r.id,
                "keeper_dict_id": r.keeper_dict_id,
                "absorbed_dict_ids": r.absorbed_dict_ids or [],
                "merge_type": r.merge_type,
                "reviewer": r.reviewer,
                "note": r.note,
                "is_rolled_back": r.is_rolled_back,
                "rolled_back_by": r.rolled_back_by,
                "rolled_back_at": r.rolled_back_at.isoformat() if r.rolled_back_at else None,
                "rollback_reason": r.rollback_reason,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
