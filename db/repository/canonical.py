"""Canonical-data merge into entity dictionary rows and canonical-to-extracted synchronisation."""

from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from db.models import EntityDict, EntityMergeQueue, ExtractedEntity

from db.repository.audit import _write_operation_log


# ── 来源优先级（canonical_data 合并用）──────────────────────────────────
_SOURCE_PRIORITY: dict[str, int] = {
    "nmpa": 10, "fda": 10, "fda_press": 10, "fda_drug_safety": 10,
    "fda_api_drug_label": 10, "fda_api_enforcement": 10,
    "drugbank": 10,
    "chictr": 8, "clinicaltrials": 8,
    "hkex": 6,
    "cde": 5, "cde_zdyz": 5,
    "pubmed": 4,
    "fiercepharma": 3, "phirda_policy": 3, "phirda_industry": 3,
    "phirda_rd": 3, "phirda_healthcare_reform": 3, "roche": 3,
}


# 数组字段：追加去重，永不覆盖
_ARRAY_FIELDS = frozenset({"aliases", "types", "innovation_labels", "categories", "atc_codes", "synonyms"})


# 快照字段：时间新的覆盖旧的（值本身是 dict，含 timestamp key）
_SNAPSHOT_FIELDS = frozenset({"market_cap", "price", "stock_price"})


def merge_canonical_data(dict_id: int, new_data: dict, source_site: str) -> None:
    """
    将新抽取的数据合并到 entity_dict.canonical_data。

    合并规则：
      - 数组字段（aliases/types 等）：追加去重，永不覆盖
      - 状态字段（phase_cn/global_status 等）：高优先级来源覆盖低优先级
          优先级：nmpa/fda/drugbank=10 > chictr/clinicaltrials=8 > hkex=6 > 行业新闻=3
      - 快照字段（market_cap 等，值为含 timestamp 的 dict）：时间新的覆盖旧的
      - 其他字段：高优先级来源覆盖低优先级（同状态字段规则）
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        entity = session.query(EntityDict).filter(EntityDict.id == dict_id).first()
        if entity is None:
            logger.warning(f"[DB] merge_canonical_data: entity_dict {dict_id} 不存在")
            return

        current = dict(entity.canonical_data or {})
        current_priority = _SOURCE_PRIORITY.get(entity.canonical_source or "", 0)
        new_priority = _SOURCE_PRIORITY.get(source_site, 1)

        merged = current.copy()
        for key, new_val in new_data.items():
            if key in _ARRAY_FIELDS:
                # 数组字段：追加去重
                existing = merged.get(key, [])
                if isinstance(existing, list) and isinstance(new_val, list):
                    merged[key] = list(dict.fromkeys(existing + new_val))  # 保序去重
                else:
                    merged[key] = new_val
            elif key in _SNAPSHOT_FIELDS:
                # 快照字段：时间新的覆盖旧的
                existing = merged.get(key, {})
                if (
                    isinstance(new_val, dict)
                    and isinstance(existing, dict)
                    and new_val.get("timestamp", 0) > existing.get("timestamp", 0)
                ):
                    merged[key] = new_val
                elif key not in merged:
                    merged[key] = new_val
            else:
                # 状态字段 / 普通字段：高优先级来源覆盖
                if key not in merged or new_priority >= current_priority:
                    merged[key] = new_val

        from db.identity import set_dictionary_identity_data
        set_dictionary_identity_data(session, entity, canonical_data=merged)
        entity.canonical_source = source_site
        entity.canonical_updated_at = datetime.now(timezone.utc)
        _write_operation_log(
            session, "update", "entity_dict", dict_id,
            business_action="merge",
            description=f"canonical_data 合并: source={source_site}",
            details={"source_site": source_site, "canonical_data": dict(entity.canonical_data or {})},
            trigger_source="merge_canonical_data",
        )


def get_entity_dict_by_id(dict_id: int) -> Optional[EntityDict]:
    """根据 ID 获取实体字典条目。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return None
        return (
            session.query(EntityDict)
            .filter(EntityDict.id == dict_id, EntityDict.is_deleted == False)
            .first()
        )


def _is_empty_value(val) -> bool:
    """值是否语义为空：None、空串、空数组、空对象、或 {"_val": 空值}。0 / False 不算空。"""
    if val is None:
        return True
    if isinstance(val, str) and val == "":
        return True
    if isinstance(val, (list, tuple)) and len(val) == 0:
        return True
    if isinstance(val, dict):
        if len(val) == 0:
            return True
        if list(val.keys()) == ["_val"]:
            return _is_empty_value(val["_val"])
    return False


# 这些字段合并风险低，差异直接跳过不排队
_SKIP_MERGE_FIELDS = frozenset({"name", "name_en", "aliases", "source_text"})


# entity_type → canonical_data 合法字段白名单
# 只有白名单内的字段才能写入 entity_dict.canonical_data，防止跨实体类型字段污染。
# 不在白名单中的 entity_type 不限制（向后兼容）。
_VALID_FIELDS_BY_TYPE: dict[str, frozenset] = {
    "Company": frozenset({
        "id", "name", "name_en", "country", "stock_code", "exchange",
        "founded_year", "types", "aliases", "market_cap", "market_cap_date",
        "global_status", "primary_focus", "innovation_labels", "stage",
    }),
    "Drug": frozenset({
        "id", "name", "name_en", "cas_number", "drug_class", "modality",
        "phase_cn", "phase_eu", "phase_jp", "phase_us",
        "patent_expiry_cn", "patent_expiry_us", "types", "aliases",
        "global_status", "innovation_type", "atc_codes", "synonyms",
    }),
    "Target": frozenset({
        "id", "name", "name_en", "gene_symbol", "uniprot_id",
        "types", "aliases", "global_status",
    }),
    "Indication": frozenset({
        "id", "name", "name_en", "types", "aliases", "icd_code",
    }),
    "ApprovalEvent": frozenset({
        "id", "name", "name_en", "doc_number", "issue_date",
        "types", "aliases",
    }),
    "Policy": frozenset({
        "id", "name", "name_en", "policy_type", "doc_number",
        "issue_date", "effective_date", "affected_regions",
        "types", "aliases",
    }),
    "ClinicalTrial": frozenset({
        "id", "name", "name_en", "phase_cn", "nct_number", "status",
        "types", "aliases",
    }),
    "DealEvent": frozenset({
        "id", "name", "name_en", "deal_type", "deal_value",
        "types", "aliases",
    }),
    "TCMFormula": frozenset({
        "id", "name", "name_en", "types", "aliases",
    }),
    "Compound": frozenset({
        "id", "name", "name_en", "cas_number", "types", "aliases",
    }),
    "Pathway": frozenset({
        "id", "name", "name_en", "types", "aliases",
    }),
}


def process_entity_merge(
    session,
    dict_id: int,
    source_entity_id: int,
    entity_data: dict,
    source_site: str,
) -> tuple[int, int]:
    """
    将 entity_data 合并到 entity_dict.canonical_data。
    返回 (auto_merged_count, conflict_count)。

    合并策略：
      - entity_dict 字段为空，新值不为空 → auto merge
      - 数组字段 → 追加去重（auto merge）
      - 快照字段 → 时间新的胜出（auto merge）
      - 新来源优先级 >= 当前来源 → auto merge
      - 同优先级不同源 → 进 entity_merge_queue 人工审核

    跳过的字段（_SKIP_MERGE_FIELDS）：name / name_en / aliases（由 alias 回写处理）
    """
    entity = session.query(EntityDict).filter(EntityDict.id == dict_id).first()
    if entity is None:
        logger.warning(f"[DB] process_entity_merge: entity_dict {dict_id} 不存在")
        return 0, 0

    current = dict(entity.canonical_data or {})
    current_priority = _SOURCE_PRIORITY.get(entity.canonical_source or "", 0)
    new_priority = _SOURCE_PRIORITY.get(source_site, 1)
    valid_fields = _VALID_FIELDS_BY_TYPE.get(entity.entity_type)  # None = 不限制

    # 查找现有值的来源实体（用于冲突记录时标记双方来源文档）
    existing_source = (
        session.query(ExtractedEntity)
        .filter(
            ExtractedEntity.dict_id == dict_id,
            ExtractedEntity.id != source_entity_id,
        )
        .order_by(ExtractedEntity.id.desc())
        .first()
    )
    existing_source_entity_id: int | None = existing_source.id if existing_source else None
    existing_source_doc_uuid: str = existing_source.source_doc_uuid if existing_source else ""

    auto_count = 0
    conflict_count = 0

    for key, new_val in entity_data.items():
        # 跳过内部元数据、_SKIP_MERGE_FIELDS、空值、非白名单字段
        if key.startswith("_") or key in _SKIP_MERGE_FIELDS or new_val is None:
            continue
        if valid_fields is not None and key not in valid_fields:
            continue

        existing_val = current.get(key)

        # 1) entity_dict 无此字段 → 直接补充
        if existing_val is None:
            current[key] = new_val
            auto_count += 1
            continue

        # 2) 数组字段：追加去重
        if key in _ARRAY_FIELDS:
            if isinstance(existing_val, list) and isinstance(new_val, list):
                merged = list(dict.fromkeys(existing_val + new_val))
                if merged != existing_val:
                    current[key] = merged
                    auto_count += 1
            continue

        # 3) 快照字段：时间新的覆盖旧的
        if key in _SNAPSHOT_FIELDS:
            if (
                isinstance(new_val, dict)
                and isinstance(existing_val, dict)
                and new_val.get("timestamp", 0) > existing_val.get("timestamp", 0)
            ):
                current[key] = new_val
                auto_count += 1
            continue

        # 4) 一侧语义为空 → 有值侧胜出
        existing_empty = _is_empty_value(existing_val)
        new_empty = _is_empty_value(new_val)
        if existing_empty and not new_empty:
            current[key] = new_val
            auto_count += 1
            continue
        if new_empty and not existing_empty:
            continue  # 保留现有值，新值为空
        if existing_empty and new_empty:
            continue  # 都为空，无冲突

        # 5) 值相同 → 跳过
        if existing_val == new_val:
            continue

        # 6) 新来源优先级严格高于当前来源 → auto merge
        if new_priority > current_priority:
            current[key] = new_val
            auto_count += 1
            continue

        # 7) 同优先级不同源 → 人工审核
        if new_priority == current_priority:
            existing_record = session.query(EntityMergeQueue).filter(
                EntityMergeQueue.target_dict_id == dict_id,
                EntityMergeQueue.field_name == key,
                EntityMergeQueue.resolution == "pending",
            ).first()
            if existing_record is None:
                session.add(EntityMergeQueue(
                    source_entity_id=source_entity_id,
                    target_dict_id=dict_id,
                    field_name=key,
                    existing_value=existing_val if isinstance(existing_val, (dict, list)) else {"_val": existing_val},
                    new_value=new_val if isinstance(new_val, (dict, list)) else {"_val": new_val},
                    source_site=source_site,
                    existing_source_entity_id=existing_source_entity_id,
                    existing_source_doc_uuid=existing_source_doc_uuid,
                    resolution="pending",
                ))
                conflict_count += 1
            else:
                # 同字段已有 pending 冲突 → 刷新值，保留最新
                existing_record.existing_value = existing_val if isinstance(existing_val, (dict, list)) else {"_val": existing_val}
                existing_record.new_value = new_val if isinstance(new_val, (dict, list)) else {"_val": new_val}
                existing_record.source_entity_id = source_entity_id
                existing_record.source_site = source_site
                existing_record.existing_source_entity_id = existing_source_entity_id
                existing_record.existing_source_doc_uuid = existing_source_doc_uuid
                logger.debug(
                    f"[DB] process_entity_merge: 刷新 pending 冲突 "
                    f"dict_id={dict_id} field={key}"
                )
            continue

        # 8) 低优来源 → 跳过，不覆盖
        pass

    if auto_count > 0:
        from db.identity import set_dictionary_identity_data
        set_dictionary_identity_data(session, entity, canonical_data=current)
        entity.canonical_updated_at = datetime.now(timezone.utc)

    if auto_count or conflict_count:
        logger.info(
            f"[DB] process_entity_merge: dict_id={dict_id} "
            f"auto_merged={auto_count} conflicts={conflict_count}"
        )
        _write_operation_log(
            session, "update", "entity_dict", dict_id,
            business_action="merge",
            description=f"canonical_data 合并: auto={auto_count} conflicts={conflict_count} source_entity={source_entity_id}",
            details={"source_entity_id": source_entity_id, "source_site": source_site,
                      "auto_merged": auto_count, "conflicts": conflict_count,
                      "canonical_data": dict(entity.canonical_data or {})},
            trigger_source="process_entity_merge",
        )

    return auto_count, conflict_count


def _sync_canonical_to_extracted(session, dict_id: int) -> int:
    """将 entity_dict.canonical_data 回写到所有已审批的 extracted_entities 行。"""
    entity_dict = session.query(EntityDict).filter(EntityDict.id == dict_id).first()
    if entity_dict is None or not entity_dict.canonical_data:
        return 0

    rows = (
        session.query(ExtractedEntity)
        .filter(
            ExtractedEntity.dict_id == dict_id,
            ExtractedEntity.review_status.in_(["approved", "auto_approved"]),
            ExtractedEntity.is_deleted == False,
        )
        .all()
    )

    if not rows:
        return 0

    cd = dict(entity_dict.canonical_data or {})
    count = 0
    for ent in rows:
        ed = dict(ent.entity_data or {})
        changed = False
        for k, v in cd.items():
            if v is not None and ed.get(k) != v:
                ed[k] = v
                changed = True
        if changed:
            ent.entity_data = ed
            count += 1

    if count:
        from db.identity import invalidate_dictionary_confirmation
        invalidate_dictionary_confirmation(session, entity_dict)
        logger.info(
            f"[DB] _sync_canonical_to_extracted: dict_id={dict_id} "
            f"synced {count}/{len(rows)} rows, {len(cd)} canonical fields"
        )

    return count
