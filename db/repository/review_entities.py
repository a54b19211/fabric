"""Human review of extracted entities: approve, reject and their batch forms."""

from datetime import datetime, timezone

from loguru import logger

from db.models import (
    EntityDict,
    EntityMergeLog,
    EntityRelation,
    ExtractedEntity,
)

from db.repository.audit import _write_operation_log, _write_review_log
from db.repository.canonical import process_entity_merge
from db.repository.extraction_results import _create_entity_dict_from_extraction
from db.repository import aliases


def get_pending_review_entities(limit: int = 50) -> list[dict]:
    """
    查询 review_status=pending 且 is_deleted=False 的实体。
    返回字段：id, source_doc_uuid, entity_type, entity_name,
              entity_data, confidence_score, confidence_reason, source_text
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(ExtractedEntity)
            .filter(
                ExtractedEntity.review_status == "pending",
                ExtractedEntity.is_deleted == False,
            )
            .limit(limit)
            .all()
        )
        return [_entity_to_dict(r) for r in rows]


def approve_entity(id: int, reviewer: str, note: str = '', **kwargs) -> None:
    """Compatibility entry point, with the same explicit confirmation policy."""
    return approve_entity_with_alias_writeback(id, reviewer, note, **kwargs)


def _dedup_extracted_entities_by_dict_id(session, dict_id: int, keeper_id: int) -> int:
    """
    审核通过后去重：同 dict_id 下保留 keeper_id，其余软删除。
    迁移数据：entity_data → process_entity_merge，
    关系重定向到 keeper_id。冲突进 merge_queue 不阻塞去重。
    """
    duplicates = (
        session.query(ExtractedEntity)
        .filter(
            ExtractedEntity.dict_id == dict_id,
            ExtractedEntity.id != keeper_id,
            ExtractedEntity.is_deleted == False,
        )
        .all()
    )

    if not duplicates:
        return 0

    for dup in duplicates:
        # a. entity_data → process_entity_merge（冲突进 merge_queue）
        if dup.entity_data:
            process_entity_merge(
                session, dict_id, keeper_id, dict(dup.entity_data), dup.source_site or "",
            )

        # b. EntityRelation 重定向到 keeper_id
        affected_rels = (
            session.query(EntityRelation)
            .filter(
                EntityRelation.is_deleted == False,
                (EntityRelation.from_entity_id == dup.id) | (EntityRelation.to_entity_id == dup.id),
            )
            .all()
        )
        for rel in affected_rels:
            if rel.from_entity_id == dup.id:
                rel.from_entity_id = keeper_id
            if rel.to_entity_id == dup.id:
                rel.to_entity_id = keeper_id

        # c. 软删除
        dup.is_deleted = True

        # d. review log
        _write_review_log(
            session, "entity", dup.id, "dedup_merge", "system",
            f"merged into entity {keeper_id} (dict_id={dict_id})",
            {"entity_name": dup.entity_name, "dict_id": dict_id},
            {"is_deleted": True, "merged_into": keeper_id},
        )

        # e. operation log
        _write_operation_log(
            session, "update", "extracted_entities", dup.id,
            business_action="dedup",
            description=f"去重软删除: name='{dup.entity_name}' → keeper_id={keeper_id} dict_id={dict_id}",
            details={"entity_name": dup.entity_name, "dict_id": dict_id, "keeper_id": keeper_id,
                      "is_deleted": True, "relations_redirected": len(affected_rels)},
            trigger_source="_dedup_extracted_entities_by_dict_id",
        )

    session.flush()

    # 去重软删除后重算受影响文档的计数
    affected_docs = {dup.source_doc_uuid for dup in duplicates if dup.source_doc_uuid}
    if affected_docs:
        from db.review_lifecycle import recalc_document_state
        for doc_uuid in affected_docs:
            recalc_document_state(session, doc_uuid)

    return len(duplicates)


def approve_entity_with_alias_writeback(entity_id, reviewer, note='', dict_id=None, force_new=False,
                                        confirm_identity=False, basis='', evidence=None, session=None, review_context=None):
    from db.identity import (use_session, require_human_reviewer, validate_evidence, get_object_evidence,
                             record_evidence, link_entity, has_candidates, snapshot, review_object_binding, refresh_link_dependencies)
    from api.review_auth import require_trusted_review_context
    if confirm_identity:
        reviewer = require_trusted_review_context(review_context)
    else:
        reviewer = require_human_reviewer(reviewer)
    if confirm_identity and not basis.strip():
        raise ValueError('Identity confirmation requires an explicit basis')
    # Fixed-version reads happen before source row locks. Recheck the source snapshot below.
    with use_session(session) as active:
        ent = active.get(ExtractedEntity, entity_id)
        if ent is None or ent.is_deleted:
            raise ValueError('Entity does not exist')
        expected = snapshot(ent)
        expected_target_id = None if force_new else (dict_id if dict_id is not None else ent.dict_id)
        target_before = active.get(EntityDict, expected_target_id) if expected_target_id else None
        expected_target = snapshot(target_before) if target_before else None
        recorded_evidence = get_object_evidence('entity', entity_id, session=active)
        allowed_sources = {(ent.source_doc_uuid, ent.source_site)} | {
            (item['source_doc_uuid'], item['source_site']) for item in recorded_evidence}
        stored_evidence = evidence or recorded_evidence
    validated = validate_evidence(stored_evidence, expected['source_doc_uuid'], expected['source_site'],
                                  allowed_sources=allowed_sources) if confirm_identity else []
    with use_session(session) as active:
        ent = active.query(ExtractedEntity).filter(ExtractedEntity.id == entity_id).populate_existing().with_for_update().first()
        if ent is None or ent.is_deleted or snapshot(ent) != expected:
            raise ValueError('Entity changed during evidence validation')
        target_id = None if force_new else (dict_id if dict_id is not None else ent.dict_id)
        if ent.review_status == 'approved' and ent.dict_id and (force_new or target_id != ent.dict_id):
            raise ValueError('Use a reversible identity proposal to change an approved identity')
        if not target_id and not confirm_identity and not force_new:
            before_status = ent.review_status
            ent.review_status = 'approved'; ent.reviewer_note = note
            ent.reviewed_by = reviewer; ent.reviewed_at = datetime.now(timezone.utc)
            _write_review_log(active, 'entity', entity_id, 'approve', reviewer, note,
                              {'review_status': before_status, 'dict_id': None},
                              {'review_status': 'approved', 'dict_id': None, 'confirm_identity': False})
            from db.review_lifecycle import recalc_document_state
            recalc_document_state(active, ent.source_doc_uuid)
            return {'success': True, 'entity_id': entity_id, 'dict_id': None, 'identity_confirmed': False}
        if not target_id:
            if has_candidates({'entity_data': ent.entity_data}) and not force_new:
                raise ValueError('Resolve the candidate link or explicitly choose a new identity first')
            target_id = _create_entity_dict_from_extraction(active, ent.entity_name, ent.entity_type,
                dict(ent.entity_data or {}), source_site=ent.source_site or '', is_verified=False,
                source_doc_uuid=ent.source_doc_uuid or '', force_create=force_new)
        target = active.query(EntityDict).filter(EntityDict.id == target_id).populate_existing().with_for_update().first()
        if target is None or target.is_deleted or target.entity_type != ent.entity_type:
            raise ValueError('Invalid identity dictionary')
        if expected_target_id and snapshot(target) != expected_target:
            raise ValueError('Dictionary identity changed during evidence validation')
        identity_link_log_id = None
        if ent.dict_id != target_id:
            reviewer = require_trusted_review_context(review_context)
            identity_link_log_id = link_entity(active, ent, target_id, reviewer, note, review_context=review_context)
        before = {'review_status': ent.review_status, 'dict_id': ent.dict_id,
                  'is_verified': target.is_verified, 'verified_by': target.verified_by}
        now = datetime.now(timezone.utc)
        ent.review_status = 'approved'; ent.reviewer_note = note; ent.reviewed_by = reviewer; ent.reviewed_at = now
        # Human-approved content may update canonical fields, but does not confer identity confirmation.
        process_entity_merge(active, target_id, ent.id, dict(ent.entity_data or {}), ent.source_site or '')
        aliases._write_aliases_with_embedding(active, target_id, ent.entity_type, ent.entity_name,
                                       dict(ent.entity_data or {}), source_doc_uuid=ent.source_doc_uuid or '',
                                       allow_embedding_generation=False)
        if confirm_identity:
            target.is_verified = True; target.verified_by = reviewer; target.verified_at = now
            target.verification_basis = basis.strip()
            ent.identity_confirmed_by = reviewer; ent.identity_confirmed_at = now
            ent.identity_confirmation_basis = basis.strip()
            record_evidence(active, 'entity', ent, {'entity_type': ent.entity_type,
                'entity_name': ent.entity_name, 'entity_data': ent.entity_data, 'evidence': validated}, ent.source_doc_uuid, ent.source_site)
        _write_review_log(active, 'entity', entity_id, 'confirm_identity' if confirm_identity else 'approve',
                          reviewer, basis if confirm_identity else note, before,
                          {'review_status': 'approved', 'dict_id': target_id, 'confirm_identity': confirm_identity,
                           'is_verified': target.is_verified, 'verified_by': target.verified_by,
                           'verified_at': target.verified_at.isoformat() if target.verified_at else None,
                           'basis': basis if confirm_identity else '', 'evidence': validated,
                           'review_audit_schema': 'trusted_human_identity_confirmation.v1' if confirm_identity else None,
                           'identity_confirmed_by': ent.identity_confirmed_by if confirm_identity else None,
                           'identity_confirmed_at': ent.identity_confirmed_at.isoformat() if confirm_identity else None,
                           'object_binding_hash': review_object_binding(ent) if confirm_identity else None})
        if identity_link_log_id is not None:
            # Approval and optional confirmation belong to this reversible single-mention change.
            identity_link_log = active.get(EntityMergeLog, identity_link_log_id)
            identity_link_log.after_snapshot = {**identity_link_log.after_snapshot, 'entities': [snapshot(ent)]}
            refresh_link_dependencies(active, identity_link_log)
        from db.review_lifecycle import recalc_document_state
        recalc_document_state(active, ent.source_doc_uuid)
        return {'success': True, 'entity_id': entity_id, 'dict_id': target_id, 'identity_confirmed': confirm_identity}


def reject_entity(id: int, reviewer: str, note: str = "") -> None:
    """审核拒绝实体，同时级联拒绝关联的 pending 关系，写 review_log。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        ent = session.query(ExtractedEntity).filter(ExtractedEntity.id == id).first()
        if ent is None:
            logger.warning(f"[DB] reject_entity: 实体 {id} 不存在")
            return
        from db.identity import clear_entity_confirmation
        clear_entity_confirmation(ent)
        before = ent.review_status
        ent.review_status = "rejected"
        ent.reviewer_note = note
        ent.reviewed_by = reviewer
        ent.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        _write_review_log(session, "entity", id, "reject", reviewer, note,
                          {"review_status": before}, {"review_status": "rejected"})
        _write_operation_log(
            session, "update", "extracted_entities", id,
            business_action="reject",
            description=f"审核拒绝: name='{ent.entity_name}' type={ent.entity_type}",
            details={"review_status": {"before": before, "after": "rejected"}, "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="reject_entity",
        )
        from db.review_lifecycle import recalc_document_state
        recalc_document_state(session, ent.source_doc_uuid)

        # 级联拒绝所有关联此实体的 pending 关系
        affected_relations = (
            session.query(EntityRelation)
            .filter(
                EntityRelation.review_status == "pending",
                EntityRelation.is_deleted == False,
                (EntityRelation.from_entity_id == id) | (EntityRelation.to_entity_id == id),
            )
            .all()
        )
        for rel in affected_relations:
            rel.review_status = "rejected"
            rel.reviewed_by = reviewer
            rel.reviewer_note = f"端点实体 {id} 被拒绝，级联拒绝"
            rel.reviewed_at = datetime.now(timezone.utc)

        if affected_relations:
            session.flush()
            for rel in affected_relations:
                _write_operation_log(
                    session, "update", "entity_relations", rel.id,
                    business_action="reject",
                    description=f"级联拒绝: from={rel.from_entity_id}→to={rel.to_entity_id} (端点实体 {id} 被拒)",
                    details={"entity_id": id, "relation_type": rel.relation_type,
                              "from_entity_id": rel.from_entity_id, "to_entity_id": rel.to_entity_id},
                    operator=reviewer,
                    trigger_source="reject_entity",
                )
            logger.info(
                f"[DB] reject_entity: entity_id={id} 级联拒绝 {len(affected_relations)} 条关系"
            )


def batch_approve_entities(
    source_doc_uuid: str = "",
    entity_type: str = "",
    source_site: str = "",
    reviewer: str = "",
    note: str = "",
    min_confidence: float = 0.0
) -> dict:
    """批量审核通过实体，支持按条件筛选。"""
    from db.identity import require_human_reviewer
    require_human_reviewer(reviewer)
    from db.session import get_session
    
    with get_session() as session:
        if session is None:
            return {"success": False, "message": "数据库未连接", "count": 0}
        
        # 构建查询条件
        q = session.query(ExtractedEntity).filter(
            ExtractedEntity.review_status == "pending",
            ExtractedEntity.is_deleted == False,
            ExtractedEntity.confidence_score >= min_confidence
        )
        
        if source_doc_uuid:
            q = q.filter(ExtractedEntity.source_doc_uuid == source_doc_uuid)
        if entity_type:
            q = q.filter(ExtractedEntity.entity_type == entity_type)
        if source_site:
            q = q.filter(ExtractedEntity.source_site == source_site)
        
        # Unresolved links remain pending until the reviewer chooses an identity.
        from db.identity import has_candidates
        entities = [ent for ent in q.all() if ent.dict_id or not has_candidates({'entity_data': ent.entity_data})]
        count = len(entities)
        
        if count == 0:
            return {"success": True, "message": "没有符合条件的待审核实体", "count": 0}
        
        # 批量更新
        affected_dict_ids: set[int] = set()
        for ent in entities:
            before = ent.review_status
            ent.review_status = "approved"
            ent.reviewer_note = note
            ent.reviewed_by = reviewer
            ent.reviewed_at = datetime.now(timezone.utc)
            _write_review_log(session, "entity", ent.id, "approve", reviewer, note,
                            {"review_status": before}, {"review_status": "approved"})

            entity_data = ent.entity_data or {}
            dict_id = ent.dict_id
            if not dict_id:
                dict_id = _create_entity_dict_from_extraction(
                    session,
                    entity_name=ent.entity_name,
                    entity_type=ent.entity_type,
                    entity_data=entity_data,
                    source_site=ent.source_site or "",
                    source_doc_uuid=ent.source_doc_uuid or "",
                )
                if dict_id:
                    ent.dict_id = dict_id
                    from db.identity import clear_entity_confirmation
                    clear_entity_confirmation(ent)
                    session.flush()

            if dict_id:
                process_entity_merge(
                    session, dict_id, ent.id, entity_data, ent.source_site or "",
                )
                affected_dict_ids.add(dict_id)

        session.commit()
        _write_operation_log(
            session, "update", "extracted_entities", None,
            business_action="approve",
            description=f"批量审核通过: {count} 个实体",
            details={"count": count,
                      "source_doc_uuid": source_doc_uuid, "entity_type": entity_type,
                      "source_site": source_site, "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="batch_approve_entities",
        )
        from db.review_lifecycle import recalc_document_state
        for doc_uuid in {ent.source_doc_uuid for ent in entities if ent.source_doc_uuid}:
            recalc_document_state(session, doc_uuid)
        logger.info(f"[DB] batch_approve_entities: {count} entities approved")
        return {"success": True, "message": f"成功批量通过 {count} 个实体", "count": count}


def batch_reject_entities(
    source_doc_uuid: str = "",
    entity_type: str = "",
    source_site: str = "",
    reviewer: str = "",
    note: str = ""
) -> dict:
    """批量审核拒绝实体，支持按条件筛选。"""
    from db.session import get_session
    
    with get_session() as session:
        if session is None:
            return {"success": False, "message": "数据库未连接", "count": 0}
        
        # 构建查询条件
        q = session.query(ExtractedEntity).filter(
            ExtractedEntity.review_status == "pending",
            ExtractedEntity.is_deleted == False
        )
        
        if source_doc_uuid:
            q = q.filter(ExtractedEntity.source_doc_uuid == source_doc_uuid)
        if entity_type:
            q = q.filter(ExtractedEntity.entity_type == entity_type)
        if source_site:
            q = q.filter(ExtractedEntity.source_site == source_site)
        
        # 获取所有符合条件的实体
        entities = q.all()
        count = len(entities)
        
        if count == 0:
            return {"success": True, "message": "没有符合条件的待审核实体", "count": 0}
        
        # 批量更新
        for ent in entities:
            before = ent.review_status
            ent.review_status = "rejected"
            ent.reviewer_note = note
            ent.reviewed_by = reviewer
            ent.reviewed_at = datetime.now(timezone.utc)
            _write_review_log(session, "entity", ent.id, "reject", reviewer, note,
                            {"review_status": before}, {"review_status": "rejected"})
        
        session.commit()
        _write_operation_log(
            session, "update", "extracted_entities", None,
            business_action="reject",
            description=f"批量审核拒绝: {count} 个实体",
            details={"count": count,
                      "source_doc_uuid": source_doc_uuid, "entity_type": entity_type,
                      "source_site": source_site, "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="batch_reject_entities",
        )
        from db.review_lifecycle import recalc_document_state
        for doc_uuid in {ent.source_doc_uuid for ent in entities if ent.source_doc_uuid}:
            recalc_document_state(session, doc_uuid)
        return {"success": True, "message": f"成功批量拒绝 {count} 个实体", "count": count}


def _entity_to_dict(r: ExtractedEntity) -> dict:
    return {
        "id": r.id,
        "source_doc_uuid": r.source_doc_uuid,
        "entity_type": r.entity_type,
        "entity_name": r.entity_name,
        "entity_data": r.entity_data,
        "source_text": r.source_text,
        "source_version_id": r.source_version_id,
        "source_quote": r.source_quote,
        "source_char_start": r.source_char_start,
        "source_char_end": r.source_char_end,
        "first_seen_at": r.first_seen_at,
        "valid_from": r.valid_from,
        "valid_to": r.valid_to,
        "dict_id": r.dict_id,
        "identity_confirmed_by": r.identity_confirmed_by,
        "identity_confirmed_at": r.identity_confirmed_at,
        "identity_confirmation_basis": r.identity_confirmation_basis,
        "confidence_score": r.confidence_score,
        "confidence_reason": r.confidence_reason,
        "review_status": r.review_status,
        "weknora_graph_status": r.weknora_graph_status,
    }
