"""Writing extraction results (entities and relations) with dictionary matching."""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func, text

from db.models import (
    Document,
    EntityAlias,
    EntityDict,
    EntityRelation,
    ExtractedEntity,
)

from db.repository.aliases import lookup_external_id_candidates
from db.repository.audit import _write_operation_log
from db.repository import aliases


def _find_existing_entity_by_dict_id(session, dict_id: int, entity_type: str):
    """
    查找 dict_id + entity_type 对应的最老未删除 extracted_entities 行。
    """
    from db.models import ExtractedEntity

    return (
        session.query(ExtractedEntity)
        .filter(
            ExtractedEntity.dict_id == dict_id,
            ExtractedEntity.entity_type == entity_type,
            ExtractedEntity.is_deleted == False,
        )
        .order_by(ExtractedEntity.id.asc())
        .first()
    )


def _is_stale_step_none(ent: ExtractedEntity) -> bool:
    """判断实体是否停留在「历史 step_none」：消歧结果仍为未匹配。"""
    if (ent.confidence_reason or "") == "no match in any step":
        return True
    disambig = (ent.entity_data or {}).get("_disambig") or {}
    return disambig.get("match_step") == "step_none"


def _refresh_step_metadata(ent: ExtractedEntity, dict_id: int) -> None:
    """刷新实体消歧元数据为「别名精确命中」，清除 step_none 陈旧状态。"""
    ent.confidence_reason = "stale_recompute: alias exact match"
    ent.confidence_score = 0.95
    ed = dict(ent.entity_data or {})
    disambig = dict(ed.get("_disambig") or {})
    disambig["match_step"] = "step2_exact_alias"
    disambig["status"] = "auto_matched"
    disambig["dict_id"] = dict_id
    ed["_disambig"] = disambig
    ent.entity_data = ed


def _cascade_recompute_stale_entities(session, dict_id: int) -> int:
    """
    dict 合并/审核通过后，级联重算历史 step_none 实体（状态陈旧修复）。

    背景：历史抽取时 step_none 的实体创建了占位 dict；当该占位 dict 被合并/
    审核归并到权威 dict（或权威 dict 新增同名 alias）后，这些历史实体的
    dict_id / match_step 仍停留在「未匹配」，未跟随字典更新。

    本函数把「entity_name 精确命中目标 dict 的 alias、且仍处于历史 step_none」
    的 pending 实体重定向到目标 dict，并刷新 match_step 为 step2_exact_alias。
    返回归位数量。
    """
    target = (
        session.query(EntityDict)
        .filter(EntityDict.id == dict_id, EntityDict.is_deleted == False)
        .first()
    )
    if target is None:
        return 0

    alias_names = [
        row[0]
        for row in session.query(EntityAlias.name)
        .filter(EntityAlias.dict_id == dict_id, EntityAlias.is_deleted == False)
        .all()
    ]
    lower_names = {n.lower() for n in alias_names if n and n.strip()}
    if not lower_names:
        return 0

    candidates = (
        session.query(ExtractedEntity)
        .filter(
            ExtractedEntity.is_deleted == False,
            ExtractedEntity.review_status == "pending",
            ExtractedEntity.entity_type == target.entity_type,
            func.lower(ExtractedEntity.entity_name).in_(lower_names),
        )
        .all()
    )

    recomputed = 0
    for ent in candidates:
        if not _is_stale_step_none(ent):
            continue

        old_dict_id = ent.dict_id
        ent.dict_id = dict_id
        from db.identity import clear_entity_confirmation
        clear_entity_confirmation(ent)
        _refresh_step_metadata(ent, dict_id)
        recomputed += 1

        _write_operation_log(
            session, "update", "extracted_entities", ent.id,
            business_action="stale_recompute",
            description=(
                f"级联重算: name='{ent.entity_name}' type={ent.entity_type} "
                f"dict_id {old_dict_id} → {dict_id}（别名精确命中）"
            ),
            details={"entity_name": ent.entity_name, "entity_type": ent.entity_type,
                      "old_dict_id": old_dict_id, "new_dict_id": dict_id},
            trigger_source="_cascade_recompute_stale_entities",
        )

    if recomputed:
        logger.info(
            f"[DB] _cascade_recompute_stale_entities: dict_id={dict_id} "
            f"重算 {recomputed} 条历史 step_none 实体"
        )

    return recomputed


def insert_extraction_result(
    source_doc_uuid: str,
    source_site: str,
    entities: list[dict],
    relations: list[dict],
    session=None,
) -> list[int]:
    """
    批量写入 LLM 抽取结果（实体 + 关系），同一事务。

    去重逻辑：消歧将实体匹配到已有 dict_id 且 entity_name 已作为 alias
    存在于该 dict_id 下时（即 Step 2 别名精确命中），不插入新行，
    而是更新已有行的 entity_data、追加 alias、重置同步状态。

    参数：
        entities:  [ExtractedEntityRecord.model_dump(), ...]
                   必须字段：entity_type, entity_name
                   可选字段：entity_data, source_text, dict_id,
                             confidence_score, confidence_reason
                   entity_data["_disambig"] 含消歧元数据 status/match_step
        relations: [ExtractedRelationRecord.model_dump(), ...]
                   必须字段：from_entity_idx, to_entity_idx, relation_type
        session:   可选，传入则复用（不 commit），不传则自开（向后兼容）

    返回：
        final_ids: 按输入顺序对应的 DB id 列表（merge 实体返回已有 id），失败返回 []
    """
    if session is not None:
        return _do_insert_extraction_result(
            session, source_doc_uuid, source_site, entities, relations
        )

    from db.session import get_session
    with get_session() as sess:
        if sess is None:
            return []
        return _do_insert_extraction_result(
            sess, source_doc_uuid, source_site, entities, relations
        )


def _lock_document_and_native_identities(session, source_doc_uuid, source_site, entities):
    """Serialise replays of one document and its strong identifiers; return the stored native projection."""
    from db.identity import digest, native_identity_claim, verified_native_identity
    # Stable across Python processes, unlike hash(). Serialize replays of one document.
    lock_key = int(digest(['extraction', source_doc_uuid])[:15], 16)
    session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
    # Native metadata is serialized input, not a trust credential. Reproject the stored original.
    native_projection = []
    if any(native_identity_claim(rec) for rec in entities):
        source = session.query(Document).filter(Document.uuid == source_doc_uuid).with_for_update().first()
        if source is not None and not source.is_deleted and source.source_site == source_site:
            from biz_rules.native_extraction import extract_native_entities, NativeExtractionError
            try:
                native_projection = extract_native_entities(source.raw_content, source.source_site)['entities']
            except NativeExtractionError:
                pass
    # Acquire all strong-ID locks in a fixed order to avoid opposite batch-order deadlocks.
    native_identities = {value for rec in entities
                         if (value := verified_native_identity(rec, source_site, native_projection))}
    for namespace, field, value in sorted(native_identities):
        session.execute(text('SELECT pg_advisory_xact_lock(:key)'),
                        {'key': int(digest(['native_identity', namespace, value])[:15], 16)})
    return native_projection


def _mark_native_identity_unverified(rec, info):
    """A native identity claim that does not match the stored original becomes an ambiguous candidate list."""
    candidates = list(info.get('candidates') or [])
    proposed_id = rec.get('dict_id') or info.get('dict_id')
    if proposed_id and not any(item.get('dict_id') == proposed_id for item in candidates):
        candidates.append({'dict_id': proposed_id, 'score': info.get('confidence', 0.0)})
    info.update(status='ambiguous', auto_link=False, dict_id=None, candidates=candidates,
                match_step='step1_native_source_unverified',
                reason='Native identity does not match the stored source projection')
    rec['dict_id'] = None


def _match_native_identity(session, rec, info, native_identifier):
    """Link by a verified native identifier: unique dictionary -> auto match, several -> ambiguous."""
    namespace, native_field, native_value = native_identifier
    matches = {}
    for key in (namespace, native_field):
        for match in lookup_external_id_candidates(rec['entity_type'], key, native_value, session=session):
            matches[match['dict_id']] = match
    candidates = [{'dict_id': match['dict_id'], 'name': match['canonical_name'],
                   'similarity_score': 1.0, 'match_type': 'native_id'} for match in matches.values()]
    selected_id = next(iter(matches)) if len(matches) == 1 else None
    info.update(status='auto_matched' if selected_id else 'ambiguous' if matches else 'new_entity',
                auto_link=bool(selected_id), dict_id=selected_id, candidates=candidates,
                match_step='step1_native_id', rule_version='native-fields.20261031.v1',
                reason='Unique native identifier' if selected_id else 'Native identifier has multiple dictionaries' if matches else 'Native identifier first observation')
    rec['dict_id'] = selected_id


def _prepare_entity_record(session, incoming, source_site, native_projection):
    """The working copy of one incoming entity with its disambiguation decision settled."""
    from db.identity import disambiguation, native_identity_claim, verified_native_identity
    rec = {**incoming, 'entity_data': dict(incoming.get('entity_data') or {})}
    info = dict(disambiguation(rec))
    native_identifier = verified_native_identity(rec, source_site, native_projection)
    if native_identity_claim(rec) and native_identifier is None:
        _mark_native_identity_unverified(rec, info)
    if native_identifier:
        _match_native_identity(session, rec, info, native_identifier)
    if info:
        rec['_disambig'] = info
        rec['entity_data']['_disambig'] = info
    return rec, info, native_identifier


def _lock_linked_dictionary(session, dict_id, rec, native_identifier):
    dictionary = session.query(EntityDict).filter(EntityDict.id == dict_id).populate_existing().with_for_update().first()
    if dictionary is None or dictionary.is_deleted or dictionary.entity_type != rec['entity_type']:
        raise ValueError('Invalid extraction dictionary link')
    if native_identifier:
        namespace, native_field, native_value = native_identifier
        if not any((dictionary.external_ids or {}).get(key) == native_value for key in (namespace, native_field)):
            raise ValueError('Native dictionary identity changed during persistence')


def _find_observed_entity(session, rec, dict_id, candidate, native_identifier, keys, batch,
                          source_doc_uuid, source_site):
    """The entity this observation already belongs to: a replay, the linked dictionary's entity, or the batch."""
    from db.identity import compatible_identity, evidence_values
    from db.models import EntitySourceEvidence
    observation = evidence_values({**rec, **((rec.get('evidence') or [{}])[0])}, source_doc_uuid, source_site)
    replay = session.query(EntitySourceEvidence).filter(
        EntitySourceEvidence.object_type == 'entity',
        EntitySourceEvidence.evidence_key == observation['evidence_key']).first()
    entity = session.get(ExtractedEntity, replay.object_id) if replay else None
    if entity is not None and entity.is_deleted:
        entity = None
    if entity is None and dict_id:
        dictionary = session.get(EntityDict, dict_id)
        if dictionary is None or dictionary.is_deleted or dictionary.entity_type != rec['entity_type']:
            raise ValueError('Invalid extraction dictionary link')
        entity = _find_existing_entity_by_dict_id(session, dict_id, rec['entity_type'])
    if entity is None and not candidate and not native_identifier:
        for previous_keys, previous_data, previous in batch:
            if (keys & previous_keys and compatible_identity(rec['entity_data'], previous_data) and
                    not (dict_id and previous.dict_id and dict_id != previous.dict_id)):
                return previous
    return entity


def _new_unreviewed_entity(session, rec, dict_id, candidate, native_identifier, source_doc_uuid, source_site):
    from db.identity import row_evidence_columns
    if dict_id is None and not candidate and (rec.get('entity_name') or '').strip():
        dict_id = _create_entity_dict_from_extraction(
            session, rec.get('entity_name', ''), rec['entity_type'], rec['entity_data'],
            source_site=source_site, is_verified=False, source_doc_uuid=source_doc_uuid,
            force_create=bool(native_identifier))
    base_evidence = {**rec, **((rec.get('evidence') or [{}])[0])}
    entity = ExtractedEntity(
        source_doc_uuid=source_doc_uuid, source_site=source_site,
        entity_type=rec['entity_type'], entity_name=rec.get('entity_name', ''),
        entity_data=rec['entity_data'], source_text=rec.get('source_text', ''), dict_id=dict_id,
        confidence_score=rec.get('confidence_score', 0.0),
        confidence_reason=rec.get('confidence_reason', ''), review_status='pending',
        **row_evidence_columns(base_evidence, source_doc_uuid, source_site))
    session.add(entity); session.flush()
    _write_operation_log(session, 'insert', 'extracted_entities', entity.id,
                         business_action='extract', description='New unreviewed entity observation',
                         details={'source_doc_uuid': source_doc_uuid, 'dict_id': dict_id},
                         trigger_source='_do_insert_extraction_result')
    return entity


def _insert_relations(session, relations, final_ids, source_doc_uuid, source_site):
    from db.identity import record_evidence, row_evidence_columns
    for incoming in relations:
        rel = dict(incoming)
        from_idx, to_idx = rel.get('from_entity_idx'), rel.get('to_entity_idx')
        if (not isinstance(from_idx, int) or not isinstance(to_idx, int) or
                not 0 <= from_idx < len(final_ids) or not 0 <= to_idx < len(final_ids)):
            raise ValueError('Relation endpoint index is outside the extraction batch')
        from_id, to_id = final_ids[from_idx], final_ids[to_idx]
        rel.update(from_entity_id=from_id, to_entity_id=to_id)
        row = session.query(EntityRelation).filter(
            EntityRelation.source_doc_uuid == source_doc_uuid,
            EntityRelation.from_entity_id == from_id, EntityRelation.to_entity_id == to_id,
            EntityRelation.relation_type == rel['relation_type'], EntityRelation.is_deleted == False).first()
        if row is None:
            base_evidence = {**rel, **((rel.get('evidence') or [{}])[0])}
            row = EntityRelation(source_doc_uuid=source_doc_uuid, source_site=source_site,
                from_entity_id=from_id, to_entity_id=to_id, relation_type=rel['relation_type'],
                relation_props=rel.get('relation_props') or {}, source_text=rel.get('source_text', ''),
                confidence_score=rel.get('confidence_score', 0.0), confidence_reason=rel.get('confidence_reason', ''),
                review_status='pending', **row_evidence_columns(base_evidence, source_doc_uuid, source_site))
            session.add(row); session.flush()
        record_evidence(session, 'relation', row, rel, source_doc_uuid, source_site)


def _do_insert_extraction_result(session, source_doc_uuid, source_site, entities, relations) -> list[int]:
    """Persist observations and link proposals without turning repeated evidence into approval."""
    from db.identity import batch_identity_keys, has_candidates, persist_candidates, record_evidence
    native_projection = _lock_document_and_native_identities(session, source_doc_uuid, source_site, entities)
    final_ids = []
    batch = []
    for incoming in entities:
        rec, info, native_identifier = _prepare_entity_record(session, incoming, source_site, native_projection)
        # Intermediate and ambiguous candidates cannot allocate a new dictionary.
        candidate = has_candidates(rec)
        dict_id = None if candidate else (rec.get('dict_id') or info.get('dict_id'))
        if dict_id:
            _lock_linked_dictionary(session, dict_id, rec, native_identifier)
        keys = batch_identity_keys(rec)
        entity = _find_observed_entity(session, rec, dict_id, candidate, native_identifier, keys, batch,
                                       source_doc_uuid, source_site)
        if entity is None:
            entity = _new_unreviewed_entity(session, rec, dict_id, candidate, native_identifier,
                                            source_doc_uuid, source_site)
        # No approval, canonical overwrite, alias migration or historical bulk repair here.
        observation_key = record_evidence(session, 'entity', entity, rec, source_doc_uuid, source_site)
        if info.get('candidates') or info.get('dict_id'):
            persist_candidates(session, entity, rec, observation_key)
        batch.append((keys, rec['entity_data'], entity))
        final_ids.append(entity.id)
    _insert_relations(session, relations, final_ids, source_doc_uuid, source_site)
    doc = session.query(Document).filter(Document.uuid == source_doc_uuid).first()
    if doc:
        doc.extraction_status = 'done'; doc.extracted_at = datetime.now(timezone.utc)
    from db.review_lifecycle import recalc_document_state
    recalc_document_state(session, source_doc_uuid)
    return final_ids


def _extract_external_ids(entity_type: str, entity_data: dict) -> dict:
    """从 entity_data 中提取 key → value 映射，供 entity_dict.external_ids 使用。"""
    FIELD_MAP: dict[str, list[str]] = {
        "Company":       ["id", "stock_code"],
        "Drug":          ["id", "cas_number"],
        "Target":        ["id", "gene_symbol"],
        "ClinicalTrial": ["trial_id"],
        "ApprovalEvent": ["approval_number"],
        "RegulatoryApplication": ["application_number"],
        "RecallProduct":  ["recall_number", "event_id"],
        "RecallEvent":    ["recall_number", "event_id"],
        "Compound":      ["cas_number"],
        "Policy":        ["doc_number"],
        "Pathway":       ["id"],
        "Indication":    ["id", "icd_code"],
    }
    result: dict[str, str] = dict(entity_data.get("external_ids") or {})
    for f in FIELD_MAP.get(entity_type, []):
        val = entity_data.get(f)
        if val and str(val).strip():
            result[f] = str(val).strip()
    native = entity_data.get("_native") or {}
    if native.get("identity_namespace") and native.get("identity_value"):
        result[str(native["identity_namespace"])] = str(native["identity_value"])
    return result


def _create_entity_dict_from_extraction(
    session,
    entity_name: str,
    entity_type: str,
    entity_data: dict,
    source_site: str = "",
    is_verified: bool = False,
    source_doc_uuid: str = "",
    force_create: bool = False,
) -> int | None:
    """
    从抽取实体创建 entity_dict 记录，返回 dict_id。
    已存在同 entity_type + 同 canonical_name 则跳过（返回已有 id）。
    is_verified=False 用于首次抽取自动创建（未经人工审核）。
    """
    from db.identity import digest, compatible_identity
    lock_key = int(digest([entity_type, entity_name.strip().casefold()])[:15], 16)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    if not force_create:
        names = {str(value).strip().casefold() for value in (entity_name, entity_data.get('name_en')) if value}
        candidates = session.query(EntityDict).join(EntityAlias, EntityAlias.dict_id == EntityDict.id).filter(
            func.lower(EntityAlias.name).in_(names), EntityDict.entity_type == entity_type,
            EntityDict.is_deleted == False, EntityAlias.is_deleted == False).all()
        compatible = [row for row in candidates if compatible_identity(entity_data,
                      {**(row.canonical_data or {}), 'external_ids': row.external_ids or {}})]
        if len({row.id for row in compatible}) == 1:
            return compatible[0].id

    external_ids = _extract_external_ids(entity_type, entity_data)
    canonical_data = {k: v for k, v in entity_data.items() if not k.startswith("_")}
    canonical_data.setdefault("name", entity_name)

    logger.info(
        f"[DB] 创建 entity_dict: name='{entity_name}' type={entity_type} "
        f"external_ids={external_ids} source={source_site}"
    )

    entity = EntityDict(
        entity_type=entity_type,
        external_ids=external_ids,
        canonical_data=canonical_data,
        canonical_source=source_site,
        is_verified=False,
    )
    session.add(entity)
    session.flush()
    _write_operation_log(
        session, "insert", "entity_dict", entity.id,
        business_action="extract",
        description=f"新建 entity_dict: name='{entity_name}' type={entity_type} source={source_site}",
        details={"entity_name": entity_name, "entity_type": entity_type, "canonical_data": canonical_data, "is_verified": False},
        trigger_source="_create_entity_dict_from_extraction",
    )
    # 写入 alias（embedding 已在锁外预生成，从 entity_data._embeddings 缓存读取）
    aliases._write_aliases_with_embedding(session, entity.id, entity_type, entity_name, entity_data,
                                  source_doc_uuid=source_doc_uuid)
    logger.info(f"[DB] entity_dict 创建成功: dict_id={entity.id}")
    return entity.id
