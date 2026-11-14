"""Human review of extracted relations: approve, reject and their batch forms."""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import false

from graph.db.models import EntityDict, EntityRelation, ExtractedEntity
from graph.db.repository.audit import _write_operation_log, _write_review_log


def get_pending_review_relations(limit: int = 50) -> list[dict]:
    """查询 review_status=pending 且 is_deleted=False 的关系。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(EntityRelation)
            .filter(
                EntityRelation.review_status == "pending",
                EntityRelation.is_deleted == false(),
            )
            .limit(limit)
            .all()
        )
        return [_relation_to_dict(r) for r in rows]


def approve_relation(id, reviewer, note='', evidence=None, session=None, review_context=None):
    from graph.api.review_auth import require_trusted_review_context
    from graph.db.identity import (
        auditable_entity_identity,
        auditable_identity,
        get_object_evidence,
        record_evidence,
        review_endpoint_binding,
        review_object_binding,
        snapshot,
        use_session,
        validate_evidence,
    )
    reviewer = require_trusted_review_context(review_context)
    with use_session(session) as active:
        relation = active.get(EntityRelation, id)
        if relation is None or relation.is_deleted:
            raise ValueError('Relation does not exist')
        expected = snapshot(relation)
        endpoint_ids = [relation.from_entity_id, relation.to_entity_id]
        endpoint_before = {key: active.get(ExtractedEntity, key) for key in endpoint_ids}
        expected_endpoints = {key: snapshot(row) if row else None for key, row in endpoint_before.items()}
        expected_dictionaries = {}
        for endpoint in endpoint_before.values():
            dictionary = active.get(EntityDict, endpoint.dict_id) if endpoint and endpoint.dict_id else None
            if not auditable_entity_identity(endpoint) or not auditable_identity(dictionary):
                raise ValueError('Both relation endpoints require auditable human identity confirmation')
            expected_dictionaries[dictionary.id] = snapshot(dictionary)
        existing_evidence = evidence or get_object_evidence('relation', id, session=active)
    validated = validate_evidence(existing_evidence, expected['source_doc_uuid'], expected['source_site'])
    with use_session(session) as active:
        relation = active.query(EntityRelation).filter(EntityRelation.id == id).populate_existing().with_for_update().first()
        if relation is None or relation.is_deleted or snapshot(relation) != expected:
            raise ValueError('Relation changed during evidence validation')
        endpoints = active.query(ExtractedEntity).filter(ExtractedEntity.id.in_(endpoint_ids)).order_by(ExtractedEntity.id).populate_existing().with_for_update().all()
        by_id = {entity.id: entity for entity in endpoints}
        dictionaries = active.query(EntityDict).filter(EntityDict.id.in_(expected_dictionaries)).order_by(EntityDict.id).populate_existing().with_for_update().all()
        dictionary_by_id = {row.id: row for row in dictionaries}
        for endpoint_id in endpoint_ids:
            entity = by_id.get(endpoint_id)
            if entity is None or snapshot(entity) != expected_endpoints[endpoint_id]:
                raise ValueError('Relation endpoint identity changed during evidence validation')
            dictionary = dictionary_by_id.get(entity.dict_id)
            if dictionary is None or snapshot(dictionary) != expected_dictionaries.get(entity.dict_id):
                raise ValueError('Relation endpoint identity changed during evidence validation')
            if not auditable_entity_identity(entity) or not auditable_identity(dictionary):
                raise ValueError('Both relation endpoints require auditable human identity confirmation')
        before = relation.review_status
        relation.review_status = 'approved'
        relation.reviewer_note = note
        relation.reviewed_by = reviewer
        relation.reviewed_at = datetime.now(timezone.utc)
        record_evidence(active, 'relation', relation, {'relation_type': relation.relation_type,
            'from_entity_id': relation.from_entity_id, 'to_entity_id': relation.to_entity_id,
            'evidence': validated}, relation.source_doc_uuid, relation.source_site)
        _write_review_log(active, 'relation', id, 'approve', reviewer, note,
                          {'review_status': before}, {'review_status': 'approved', 'evidence': validated,
                            'review_audit_schema': 'trusted_human_relation_approval.v1',
                            'reviewed_by': relation.reviewed_by, 'reviewed_at': relation.reviewed_at.isoformat(),
                            'object_binding_hash': review_object_binding(relation),
                            'endpoint_identities': {str(key): review_endpoint_binding(by_id[key]) for key in endpoint_ids}})
        from graph.db.review_lifecycle import recalc_document_state
        recalc_document_state(active, relation.source_doc_uuid)
        return {'success': True, 'relation_id': id}


def reject_relation(id: int, reviewer: str, note: str = "") -> None:
    """审核拒绝关系，同时写 review_log。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return
        rel = session.query(EntityRelation).filter(EntityRelation.id == id).first()
        if rel is None:
            logger.warning(f"[DB] reject_relation: 关系 {id} 不存在")
            return
        before = rel.review_status
        rel.review_status = "rejected"
        rel.reviewer_note = note
        rel.reviewed_by = reviewer
        rel.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        _write_review_log(session, "relation", id, "reject", reviewer, note,
                          {"review_status": before}, {"review_status": "rejected"})
        _write_operation_log(
            session, "update", "entity_relations", id,
            business_action="reject",
            description=f"审核拒绝关系: {rel.relation_type} from={rel.from_entity_id}→to={rel.to_entity_id}",
            details={"relation_type": rel.relation_type,
                      "from_entity_id": rel.from_entity_id, "to_entity_id": rel.to_entity_id,
                      "review_status": {"before": before, "after": "rejected"}, "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="reject_relation",
        )


def batch_approve_relations(source_doc_uuid='', source_site='', reviewer='', note='',
                            min_confidence=0.0, review_context=None):
    """Apply the same fixed-evidence and endpoint checks to every selected relation."""
    from graph.api.review_auth import require_trusted_review_context
    from graph.db.identity import use_session
    from store.record_models import FabricError
    reviewer = require_trusted_review_context(review_context)
    with use_session() as session:
        query = session.query(EntityRelation).filter(EntityRelation.review_status == 'pending',
            EntityRelation.is_deleted == false(), EntityRelation.confidence_score >= min_confidence)
        if source_doc_uuid:
            query = query.filter(EntityRelation.source_doc_uuid == source_doc_uuid)
        if source_site:
            query = query.filter(EntityRelation.source_site == source_site)
        ids = [row.id for row in query.all()]
    approved, blocked = [], []
    for relation_id in ids:
        try:
            approve_relation(relation_id, reviewer, note, review_context=review_context)
            approved.append(relation_id)
        except (ValueError, FabricError) as exc:
            blocked.append({'id': relation_id, 'reason': str(exc)})
    return {'success': True, 'count': len(approved), 'blocked': blocked,
            'message': f'Approved {len(approved)} relations; {len(blocked)} require evidence or identity review'}


def batch_reject_relations(
    source_doc_uuid: str = "",
    source_site: str = "",
    reviewer: str = "",
    note: str = ""
) -> dict:
    """批量审核拒绝关系，支持按条件筛选。"""
    from graph.db.session import get_session

    with get_session() as session:
        if session is None:
            return {"success": False, "message": "数据库未连接", "count": 0}

        # 构建查询条件
        q = session.query(EntityRelation).filter(
            EntityRelation.review_status == "pending",
            EntityRelation.is_deleted == false()
        )

        if source_doc_uuid:
            q = q.filter(EntityRelation.source_doc_uuid == source_doc_uuid)
        if source_site:
            q = q.filter(EntityRelation.source_site == source_site)

        # 获取所有符合条件的实体
        relations = q.all()
        count = len(relations)

        if count == 0:
            return {"success": True, "message": "没有符合条件的待审核关系", "count": 0}

        # 批量更新
        for rel in relations:
            before = rel.review_status
            rel.review_status = "rejected"
            rel.reviewer_note = note
            rel.reviewed_by = reviewer
            rel.reviewed_at = datetime.now(timezone.utc)
            _write_review_log(session, "relation", rel.id, "reject", reviewer, note,
                            {"review_status": before}, {"review_status": "rejected"})

        session.commit()
        _write_operation_log(
            session, "update", "entity_relations", None,
            business_action="reject",
            description=f"批量审核拒绝: {count} 个关系",
            details={"count": count,
                      "source_doc_uuid": source_doc_uuid, "source_site": source_site,
                      "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="batch_reject_relations",
        )
        return {"success": True, "message": f"成功批量拒绝 {count} 个关系", "count": count}


def _relation_to_dict(r: EntityRelation) -> dict:
    return {
        "id": r.id,
        "source_doc_uuid": r.source_doc_uuid,
        "from_entity_id": r.from_entity_id,
        "to_entity_id": r.to_entity_id,
        "relation_type": r.relation_type,
        "relation_props": r.relation_props,
        "source_text": r.source_text,
        "source_version_id": r.source_version_id,
        "source_quote": r.source_quote,
        "source_char_start": r.source_char_start,
        "source_char_end": r.source_char_end,
        "first_seen_at": r.first_seen_at,
        "valid_from": r.valid_from,
        "valid_to": r.valid_to,
        "confidence_score": r.confidence_score,
        "confidence_reason": r.confidence_reason,
        "review_status": r.review_status,
        "weknora_graph_status": r.weknora_graph_status,
    }
