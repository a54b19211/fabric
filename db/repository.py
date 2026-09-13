"""
数据库仓库层 —— 所有数据操作的统一入口。

设计原则：
  - 所有模块通过此文件操作数据库，不允许直接写 SQL
  - 软删除：所有查询默认过滤 is_deleted=False
  - 幂等性：upsert 基于 uuid ON CONFLICT
  - Session 管理：使用 db/session.py 提供的 get_session()
"""
import re
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import text, func

from db.models import (
    Document, DocExtra, Attachment,
    EntityDict, EntityAlias, ExtractedEntity, EntityRelation,
    CrawlLog, ReviewLog,
    EntityMergeQueue, EntityMergeLog,
    EntityIdentityMergeProposal,
    OperationLog,
)

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

# 制剂后缀：entity_name 以此结尾时，不写入 entity_name 的子串作为 alias
_FORMULATION_SUFFIX = re.compile(
    r'(片|颗粒|试剂|胶囊|注射液|分散片|缓释片|肠溶片|滴丸|口服液'
    r'|糖浆|软胶囊|咀嚼片|泡腾片|注射用|冻干|混悬液|滴眼液'
    r'|软膏|乳膏|凝胶|栓剂|气雾剂|喷雾剂|贴剂|洗剂|搽剂)$'
)


# ── 操作日志 ───────────────────────────────────────────────────────────

def _write_operation_log(
    session,
    operation: str,
    target_table: str,
    target_id: int | None,
    business_action: str = "",
    description: str = "",
    details: dict | None = None,
    operator: str = "system",
    process: str = "",
    trigger_source: str = "",
) -> None:
    """写入一条操作日志。不抛异常，失败只记 warning。"""
    try:
        session.add(OperationLog(
            operation=operation,
            target_table=target_table,
            target_id=target_id,
            business_action=business_action,
            description=description,
            details=details or {},
            operator=operator,
            process=process,
            trigger_source=trigger_source,
        ))
    except Exception as e:
        logger.warning(f"[OperationLog] 写入失败: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 1. Document Repository
# ═══════════════════════════════════════════════════════════════════════

def upsert_document(envelope) -> None:
    """
    幂等写入文档。同一 uuid 重复调用只更新，不新增。
    同时写入：documents + doc_extra（extra 非空时）+ attachments（有附件时）。
    参数 envelope 是 MetadataEnvelope 对象。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            logger.warning("[DB] Session 未初始化，跳过 upsert_document")
            return

        # ── 1. documents 表 ──────────────────────────────────────────
        doc = session.query(Document).filter(Document.uuid == envelope.uuid).first()
        if doc is None:
            doc = Document(
                uuid=envelope.uuid,
                title=envelope.title or "",
                publish_date=envelope.publish_date or "",
                category=envelope.category or "",
                summary=envelope.summary or "",
                raw_content=envelope.raw_content or "",
                source_url=envelope.provenance.source_url or "",
                source_site=envelope.provenance.source_site or "",
                parent_uuid=envelope.provenance.parent_uuid or "",
                crawl_timestamp=_parse_ts(envelope.provenance.crawl_timestamp),
                confidence_score=envelope.confidence.score,
                confidence_reason=envelope.confidence.reason or "",
                parse_method=envelope.parse_method or "",
                doc_status=envelope.status or "AUTO_PROCESSED",
                reviewer_note=envelope.reviewer_note or "",
                doc_type=envelope.doc_type or "",
                language=envelope.language or "",
                weknora_sync_status="pending",
                extraction_status="pending",
            )
            session.add(doc)
            logger.info(f"[DB] 新增文档: {envelope.uuid}")
            is_new = True
        else:
            # 只更新可变字段，保留 extraction_status / weknora_sync_status 等流水线状态
            doc.title = envelope.title or doc.title
            doc.summary = envelope.summary or doc.summary
            doc.raw_content = envelope.raw_content or doc.raw_content
            doc.source_url = envelope.provenance.source_url or doc.source_url
            doc.parse_method = envelope.parse_method or doc.parse_method
            doc.doc_type = envelope.doc_type or doc.doc_type
            doc.language = envelope.language or doc.language
            logger.info(f"[DB] 更新文档: {envelope.uuid}")
            is_new = False

        session.flush()  # 确保 doc.id 可用
        _write_operation_log(
            session, "insert" if is_new else "update", "documents", doc.id,
            business_action="upsert",
            description=f"{'新增' if is_new else '更新'}文档: {envelope.uuid} site={envelope.provenance.source_site or ''}",
            details={"uuid": envelope.uuid, "source_site": envelope.provenance.source_site or "",
                      "doc_type": envelope.doc_type or "", "parse_method": envelope.parse_method or ""},
            trigger_source="upsert_document",
        )

        # ── 2. doc_extra 表 ──────────────────────────────────────────
        if envelope.extra:
            _upsert_doc_extra_in_session(
                session,
                uuid=envelope.uuid,
                source_site=envelope.provenance.source_site or "",
                extra=envelope.extra,
            )

        # ── 3. attachments 表（先删后插，保证幂等）───────────────────
        if envelope.attachments:
            session.query(Attachment).filter(
                Attachment.parent_uuid == envelope.uuid
            ).delete(synchronize_session=False)
            for att in envelope.attachments:
                session.add(Attachment(
                    parent_uuid=envelope.uuid,
                    file_name=att.file_name or "",
                    file_url=att.file_url or "",
                    file_uuid=att.file_uuid or "",
                    local_path=getattr(att, "local_path", "") or "",
                ))

        logger.info(f"[DB] 文档入库成功: {envelope.uuid}")


def upsert_doc_extra(uuid: str, source_site: str, extra: dict) -> None:
    """单独写入/更新 doc_extra 表。供 db/sites/ 各来源写入器调用。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        _upsert_doc_extra_in_session(session, uuid, source_site, extra)


def _upsert_doc_extra_in_session(session, uuid: str, source_site: str, extra: dict) -> None:
    """内部辅助：在已有 session 中写 doc_extra，不自行 commit。"""
    row = session.query(DocExtra).filter(DocExtra.uuid == uuid).first()
    if row is None:
        row = DocExtra(uuid=uuid, source_site=source_site, extra=extra)
        session.add(row)
        session.flush()
        _write_operation_log(
            session, "insert", "doc_extra", row.id,
            business_action="upsert",
            description=f"新增 doc_extra: uuid={uuid} site={source_site}",
            details={"uuid": uuid, "source_site": source_site},
            trigger_source="_upsert_doc_extra_in_session",
        )
    else:
        row.extra = extra
        row.source_site = source_site
        _write_operation_log(
            session, "update", "doc_extra", row.id,
            business_action="upsert",
            description=f"更新 doc_extra: uuid={uuid} site={source_site}",
            details={"uuid": uuid, "source_site": source_site},
            trigger_source="_upsert_doc_extra_in_session",
        )


def get_document_by_uuid(uuid: str) -> Optional[Document]:
    """根据 UUID 获取文档（含软删除过滤）。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return None
        return (
            session.query(Document)
            .filter(Document.uuid == uuid, Document.is_deleted == False)
            .first()
        )


def update_extraction_status(
    uuid: str,
    status: str,
    model: str = "",
    error: str = "",
    extracted_at=None,
) -> None:
    """更新文档的 LLM 抽取状态。status: pending / queued / processing / done / failed"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        doc = session.query(Document).filter(Document.uuid == uuid).first()
        if doc is None:
            logger.warning(f"[DB] update_extraction_status: 文档不存在 {uuid}")
            return
        before_status = doc.extraction_status
        doc.extraction_status = status
        if model:
            doc.extraction_model = model
        if error:
            doc.extraction_error = error
        if status == "done":
            doc.extracted_at = extracted_at or datetime.now(timezone.utc)
        _write_operation_log(
            session, "update", "documents", doc.id,
            business_action="extract",
            description=f"extraction_status: {before_status} → {status} uuid={uuid}",
            details={"uuid": uuid, "extraction_status": {"before": before_status, "after": status}, "model": model, "error": error},
            trigger_source="update_extraction_status",
        )


def reset_stale_extractions(max_age_minutes: int = 0) -> int:
    """
    将残留的 queued / processing 重置为 pending。

    max_age_minutes:
      - 0（默认）：重置所有，启动时使用
      - > 0：只重置 updated_at 超过该时间的文档，运行时使用

    适用场景：Worker 超时/崩溃后，RQ 已丢弃任务但 DB 仍显示 queued/processing。
    """
    from datetime import datetime, timedelta, timezone
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return 0
        q = session.query(Document).filter(
            Document.extraction_status.in_(["queued", "processing"]),
            Document.is_deleted == False,
        )
        if max_age_minutes > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
            q = q.filter(Document.updated_at < cutoff)

        result = q.update(
            {"extraction_status": "pending", "extraction_error": "任务超时/服务重启，状态回退"},
            synchronize_session=False,
        )
        if result:
            logger.info(f"[DB] reset_stale_extractions(max_age={max_age_minutes}m): {result} 篇回退 pending")
        return result


def update_weknora_sync_status(uuid: str, status: str) -> None:
    """更新文档的 WeKnora 同步状态。status: pending / synced / failed"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        doc = session.query(Document).filter(Document.uuid == uuid).first()
        if doc is None:
            logger.warning(f"[DB] update_weknora_sync_status: 文档不存在 {uuid}")
            return
        before_status = doc.weknora_sync_status
        doc.weknora_sync_status = status
        _write_operation_log(
            session, "update", "documents", doc.id,
            business_action="weknora_sync",
            description=f"weknora_sync_status: {before_status} → {status} uuid={uuid}",
            details={"uuid": uuid, "weknora_sync_status": {"before": before_status, "after": status}},
            trigger_source="update_weknora_sync_status",
        )


def get_pending_extraction(limit: int = 100) -> list[dict]:
    """
    查询 extraction_status=pending 且 is_deleted=False 的文档。
    返回字段：uuid, title, source_site, raw_content, doc_type
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(
                Document.uuid,
                Document.title,
                Document.source_site,
                Document.raw_content,
                Document.doc_type,
            )
            .filter(
                Document.extraction_status == "pending",
                Document.is_deleted == False,
            )
            .limit(limit)
            .all()
        )
        return [
            {
                "uuid": r.uuid,
                "title": r.title,
                "source_site": r.source_site,
                "raw_content": r.raw_content,
                "doc_type": r.doc_type,
            }
            for r in rows
        ]


def get_pending_weknora_sync(limit: int = 100, offset: int = 0, source_site: str = "") -> tuple[list[dict], int]:
    """
    查询 weknora_sync_status=pending 且 is_deleted=False 的文档。
    返回字段：uuid, title, source_site, raw_content, publish_date,
             source_url, parent_uuid, doc_type, language, confidence_score,
             parse_method, crawl_timestamp
    返回 (rows, total)。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return [], 0
        filters = [
            Document.weknora_sync_status == "pending",
            Document.is_deleted == False,
        ]
        if source_site:
            filters.append(Document.source_site == source_site)
        base_q = session.query(Document).filter(*filters)
        total = base_q.count()
        rows = (
            session.query(
                Document.uuid,
                Document.title,
                Document.source_site,
                Document.raw_content,
                Document.publish_date,
                Document.source_url,
                Document.parent_uuid,
                Document.doc_type,
                Document.language,
                Document.confidence_score,
                Document.parse_method,
                Document.crawl_timestamp,
            )
            .filter(*filters)
            .order_by(Document.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        rows_out = [
            {
                "uuid": r.uuid,
                "title": r.title,
                "source_site": r.source_site,
                "raw_content": r.raw_content or "",
                "publish_date": r.publish_date or "",
                "source_url": r.source_url or "",
                "parent_uuid": r.parent_uuid or "",
                "doc_type": r.doc_type or "",
                "language": r.language or "",
                "confidence_score": r.confidence_score or 0.0,
                "parse_method": r.parse_method or "",
                "crawl_timestamp": str(r.crawl_timestamp) if r.crawl_timestamp else "",
            }
            for r in rows
        ]
        return rows_out, total


# ═══════════════════════════════════════════════════════════════════════
# 2. Attachment Repository
# ═══════════════════════════════════════════════════════════════════════

def get_attachments(parent_uuid: str) -> list[Attachment]:
    """获取文档的附件列表（软删除过滤）。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        return (
            session.query(Attachment)
            .filter(
                Attachment.parent_uuid == parent_uuid,
                Attachment.is_deleted == False,
            )
            .all()
        )


# ═══════════════════════════════════════════════════════════════════════
# 3. EntityDict Repository
# ═══════════════════════════════════════════════════════════════════════

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

        entity.canonical_data = merged
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


# ═══════════════════════════════════════════════════════════════════════
# 3b. 实体数据合并队列
# ═══════════════════════════════════════════════════════════════════════

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
        entity.canonical_data = current
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


def create_identity_merge_proposal(
    source_entity_id: int,
    target_dict_id: int,
    agent_reason: str = "",
    agent_confidence: float = 0.0,
    agent_run_id: str = "",
) -> int | None:
    """写入身份合并提案到 entity_identity_merge_proposal，供人工审核确认。

    将 extracted_entity（source_entity_id）和 target entity_dict 的完整快照
    分别存入 source_entity_snapshot 和 target_dict_snapshot。

    Returns:
        proposal_id，失败返回 None
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return None

        source_ent = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == source_entity_id,
            ExtractedEntity.is_deleted == False,
        ).first()
        if source_ent is None:
            logger.warning(f"[DB] create_identity_merge_proposal: source_entity {source_entity_id} 不存在")
            return None

        target_dict = session.query(EntityDict).filter(
            EntityDict.id == target_dict_id,
            EntityDict.is_deleted == False,
        ).first()
        if target_dict is None:
            logger.warning(f"[DB] create_identity_merge_proposal: target_dict {target_dict_id} 不存在")
            return None

        target_aliases = [
            a.name for a in session.query(EntityAlias).filter(
                EntityAlias.dict_id == target_dict_id,
                EntityAlias.is_deleted == False,
            ).all()
        ]

        source_snapshot = {
            "entity_name": source_ent.entity_name or "",
            "entity_type": source_ent.entity_type or "",
            "entity_data": {k: v for k, v in (source_ent.entity_data or {}).items()
                           if not k.startswith("_")},
            "source_site": source_ent.source_site or "",
            "source_doc_uuid": source_ent.source_doc_uuid or "",
            "source_text": (source_ent.source_text or "")[:500],
        }

        target_snapshot = {
            "canonical_name": (target_dict.canonical_data or {}).get("name", ""),
            "entity_type": target_dict.entity_type or "",
            "aliases": target_aliases,
            "source_site": target_dict.canonical_source or "",
            "is_verified": bool(target_dict.is_verified),
            "key_fields": {
                k: v for k, v in (target_dict.canonical_data or {}).items()
                if k in ("cas_number", "unii", "atc_code", "stock_code", "approval_number",
                         "pubchem_cid", "drugbank_id", "nct_number")
            },
        }

        proposal = EntityIdentityMergeProposal(
            source_entity_id=source_entity_id,
            target_dict_id=target_dict_id,
            source_entity_snapshot=source_snapshot,
            target_dict_snapshot=target_snapshot,
            agent_run_id=agent_run_id,
            agent_reason=agent_reason,
            agent_confidence=agent_confidence,
            status="pending",
        )
        session.add(proposal)
        session.flush()
        proposal_id = proposal.id

        _write_operation_log(
            session, "insert", "entity_identity_merge_proposal", proposal_id,
            business_action="disambiguate",
            description=(
                f"Agent 身份合并提案: {source_ent.entity_name} → "
                f"dict_id={target_dict_id} confidence={agent_confidence:.2f}"
            ),
            details={
                "source_entity_id": source_entity_id,
                "target_dict_id": target_dict_id,
                "agent_run_id": agent_run_id,
                "agent_confidence": agent_confidence,
            },
            trigger_source="create_identity_merge_proposal",
        )

        logger.info(
            f"[DB] 身份合并提案已创建: proposal_id={proposal_id} "
            f"{source_ent.entity_name} → dict_id={target_dict_id}"
        )
        return proposal_id


def get_identity_merge_proposals(status: str = "pending", limit: int = 100) -> list[dict]:
    """查询身份合并提案列表。

    Args:
        status: pending | approved | rejected，默认 pending
        limit: 返回数量上限

    Returns:
        提案列表，每项包含双方快照和 Agent 决策信息
    """
    from db.session import get_session

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
                "status": r.status,
                "reviewer": r.reviewer,
                "review_note": r.review_note,
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else "",
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ]


def resolve_identity_merge_proposal(
    proposal_id: int,
    status: str,
    reviewer: str = "",
    note: str = "",
) -> bool:
    """批准或拒绝身份合并提案。

    Args:
        proposal_id: 提案 ID
        status: approved | rejected
        reviewer: 审核人
        note: 备注

    Returns:
        是否成功
    """
    from db.session import get_session

    if status not in ("approved", "rejected"):
        logger.warning(f"[DB] resolve_identity_merge_proposal: 无效状态 {status}")
        return False

    with get_session() as session:
        if session is None:
            return False

        proposal = session.query(EntityIdentityMergeProposal).filter(
            EntityIdentityMergeProposal.id == proposal_id,
        ).first()
        if proposal is None:
            logger.warning(f"[DB] resolve_identity_merge_proposal: proposal {proposal_id} 不存在")
            return False
        if proposal.status != "pending":
            logger.warning(f"[DB] resolve_identity_merge_proposal: proposal {proposal_id} 状态为 {proposal.status}")
            return False

        proposal.status = status
        proposal.reviewer = reviewer
        proposal.review_note = note
        proposal.reviewed_at = datetime.now(timezone.utc)

        _write_operation_log(
            session,
            "update",
            "entity_identity_merge_proposal",
            proposal_id,
            business_action="review",
            description=(
                f"身份合并提案 {status}: "
                f"{proposal.source_entity_snapshot.get('entity_name', '')} "
                f"→ dict_id={proposal.target_dict_id}"
            ),
            details={
                "proposal_id": proposal_id,
                "status": status,
                "reviewer": reviewer,
                "note": note,
            },
            operator=reviewer,
            trigger_source="resolve_identity_merge_proposal",
        )

        logger.info(f"[DB] 身份合并提案已{status}: proposal_id={proposal_id}")
        return True


def get_pending_merges(limit: int = 50) -> dict:
    """
    查询待处理合并冲突，按 dict_id 聚合。
    返回前校验新鲜度（批量查表，无 N+1），自动 resolve 已过期冲突。
    """
    from db.session import get_session

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
                EntityDict.is_deleted == False,
            ).all()
        }
        source_ent_map = {
            se.id: se
            for se in session.query(ExtractedEntity).filter(
                ExtractedEntity.id.in_(source_ent_ids),
                ExtractedEntity.is_deleted == False,
            ).all()
        }

        # 4. 逐条校验新鲜度
        groups: dict[int, dict] = {}
        auto_resolved = 0

        for r in rows:
            entity = dict_map.get(r.target_dict_id)
            if entity is None:
                r.resolution = "resolved_dict_deleted"
                r.reviewed_at = datetime.now(timezone.utc)
                auto_resolved += 1
                _write_operation_log(
                    session, "update", "entity_merge_queue", r.id,
                    business_action="auto_resolve",
                    description=f"dict 已删除: dict_id={r.target_dict_id} field={r.field_name}",
                    details={"reason": "dict_deleted", "dict_id": r.target_dict_id},
                    trigger_source="get_pending_merges",
                )
                continue

            source_ent = source_ent_map.get(r.source_entity_id)
            if source_ent is None:
                r.resolution = "resolved_source_deleted"
                r.reviewed_at = datetime.now(timezone.utc)
                auto_resolved += 1
                _write_operation_log(
                    session, "update", "entity_merge_queue", r.id,
                    business_action="auto_resolve",
                    description=f"source 实体已删除: entity_id={r.source_entity_id} field={r.field_name}",
                    details={"reason": "source_deleted", "source_entity_id": r.source_entity_id},
                    trigger_source="get_pending_merges",
                )
                continue

            # 值相等 / 一侧语义为空 → 自动 resolve
            canonical = entity.canonical_data or {}
            current_val = canonical.get(r.field_name)
            new_val = r.new_value.get("_val") if isinstance(r.new_value, dict) and list(r.new_value.keys()) == ["_val"] else r.new_value
            cur_empty = _is_empty_value(current_val)
            new_empty = _is_empty_value(new_val)
            if cur_empty and new_empty:
                r.resolution = "resolved_both_empty"
                r.reviewed_at = datetime.now(timezone.utc)
                auto_resolved += 1
                continue
            if cur_empty and not new_empty:
                r.resolution = "accept_new"
                r.reviewed_at = datetime.now(timezone.utc)
                auto_resolved += 1
                cd = dict(entity.canonical_data or {})
                cd[r.field_name] = r.new_value
                entity.canonical_data = cd
                entity.canonical_updated_at = datetime.now(timezone.utc)
                continue
            if new_empty and not cur_empty:
                r.resolution = "keep_existing"
                r.reviewed_at = datetime.now(timezone.utc)
                auto_resolved += 1
                continue
            if current_val == new_val:
                r.resolution = "resolved_already_merged"
                r.reviewed_at = datetime.now(timezone.utc)
                auto_resolved += 1
                _write_operation_log(
                    session, "update", "entity_merge_queue", r.id,
                    business_action="auto_resolve",
                    description=f"值已通过其他途径合并: dict_id={r.target_dict_id} field={r.field_name}",
                    details={"reason": "already_merged", "current_value": current_val},
                    trigger_source="get_pending_merges",
                )
                continue

            # 刷新过期的 existing_value
            existing_val = r.existing_value.get("_val") if isinstance(r.existing_value, dict) and list(r.existing_value.keys()) == ["_val"] else r.existing_value
            if existing_val != current_val and current_val is not None:
                r.existing_value = current_val if isinstance(current_val, (dict, list)) else {"_val": current_val}

            # 组装分组
            if r.target_dict_id not in groups:
                cd = entity.canonical_data or {}
                groups[r.target_dict_id] = {
                    "dict_id": r.target_dict_id,
                    "entity_type": entity.entity_type,
                    "entity_name": cd.get("name", ""),
                    "canonical_data": cd,
                    "canonical_source": entity.canonical_source or "",
                    "conflicts": [],
                }

            existing_val_out = r.existing_value
            new_val_out = r.new_value
            groups[r.target_dict_id]["conflicts"].append({
                "merge_id": r.id,
                "field_name": r.field_name,
                "existing_value": existing_val_out.get("_val") if isinstance(existing_val_out, dict) and list(existing_val_out.keys()) == ["_val"] else existing_val_out,
                "new_value": new_val_out.get("_val") if isinstance(new_val_out, dict) and list(new_val_out.keys()) == ["_val"] else new_val_out,
                "source_entity_id": r.source_entity_id,
                "source_site": r.source_site,
                "source_doc_uuid": source_ent.source_doc_uuid,
                "existing_source_doc_uuid": r.existing_source_doc_uuid or "",
            })

        if auto_resolved:
            session.commit()
            logger.info(f"[DB] get_pending_merges: auto resolved {auto_resolved} 条过期冲突")

        # 5. 补全来源文档列表：查出所有关联实体，按字段值分组
        group_dict_ids = list(groups.keys())
        if group_dict_ids:
            all_related_entities = (
                session.query(ExtractedEntity)
                .filter(
                    ExtractedEntity.dict_id.in_(group_dict_ids),
                )
                .all()
            )
            # entities_by_dict[dict_id] = [entity, ...]
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
                        ed = ent.entity_data or {}
                        ent_val = ed.get(c["field_name"])
                        if ent_val is None:
                            continue
                        doc = ent.source_doc_uuid or ""
                        src = {
                            "entity_id": ent.id,
                            "source_doc_uuid": doc,
                            "source_site": ent.source_site or "",
                        }
                        if ent_val == ev and doc not in seen_existing:
                            existing_sources.append(src)
                            seen_existing.add(doc)
                        elif ent_val == nv and doc not in seen_new:
                            new_sources.append(src)
                            seen_new.add(doc)
                    c["existing_sources"] = existing_sources
                    c["new_sources"] = new_sources

        sorted_groups = sorted(groups.values(), key=lambda g: g["dict_id"], reverse=True)
        return {"total_groups": len(sorted_groups), "groups": sorted_groups}


def resolve_merge(
    merge_id: int,
    resolution: str,
    reviewer: str = "",
    resolved_value: dict | None = None,
) -> None:
    """
    解决合并冲突。
    resolution: accept_new | keep_existing | custom
    custom 时需要传 resolved_value。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return

        merge = session.query(EntityMergeQueue).filter(EntityMergeQueue.id == merge_id).first()
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
                canonical[merge.field_name] = merge.new_value
                entity.canonical_data = canonical
                entity.canonical_updated_at = datetime.now(timezone.utc)
        elif resolution == "custom" and resolved_value is not None:
            merge.resolved_value = resolved_value
            entity = session.query(EntityDict).filter(EntityDict.id == merge.target_dict_id).first()
            if entity:
                canonical = dict(entity.canonical_data or {})
                canonical[merge.field_name] = resolved_value
                entity.canonical_data = canonical
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
) -> dict:
    """
    批量解决合并冲突。一次请求解决同 dict_id 下所有冲突字段。
    decisions: [{merge_id, resolution, resolved_value?}]
    """
    from db.session import get_session

    with get_session() as session:
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
            ).first()
            if merge is None:
                continue

            merge.resolution = resolution
            merge.reviewed_by = reviewer
            merge.reviewed_at = datetime.now(timezone.utc)

            if resolution == "accept_new":
                entity = session.query(EntityDict).filter(
                    EntityDict.id == merge.target_dict_id,
                    EntityDict.is_deleted == False,
                ).first()
                if entity:
                    canonical = dict(entity.canonical_data or {})
                    new_val = merge.new_value.get("_val") if isinstance(merge.new_value, dict) and list(merge.new_value.keys()) == ["_val"] else merge.new_value
                    canonical[merge.field_name] = new_val
                    entity.canonical_data = canonical
                    entity.canonical_updated_at = datetime.now(timezone.utc)
                    affected_dict_ids.add(merge.target_dict_id)
            elif resolution == "keep_existing":
                pass
            elif resolution == "custom" and resolved_value is not None:
                merge.resolved_value = resolved_value
                entity = session.query(EntityDict).filter(
                    EntityDict.id == merge.target_dict_id,
                    EntityDict.is_deleted == False,
                ).first()
                if entity:
                    canonical = dict(entity.canonical_data or {})
                    canonical[merge.field_name] = resolved_value
                    entity.canonical_data = canonical
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

        session.commit()
        logger.info(f"[DB] batch_resolve_merges: resolved {resolved_count} conflicts")

        return {"success": True, "resolved_count": resolved_count}


# ═══════════════════════════════════════════════════════════════════════
# 3c. 实体搜索 & 合并工作台
# ═══════════════════════════════════════════════════════════════════════

def search_entity_dicts(q: str, limit: int = 20) -> list[dict]:
    """模糊搜索 entity_dict：按名称或别名匹配。"""
    from db.session import get_session

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
                EntityAlias.is_deleted == False,
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
                EntityDict.is_deleted == False,
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
                    EntityDict.is_deleted == False,
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
            EntityAlias.is_deleted == False,
        ).all()
    ]
    count = session.query(ExtractedEntity).filter(
        ExtractedEntity.dict_id == entity.id,
        ExtractedEntity.is_deleted == False,
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
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return {"entities": [], "field_diff": []}

        entities_raw = (
            session.query(EntityDict)
            .filter(EntityDict.id.in_(dict_ids), EntityDict.is_deleted == False)
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
                    EntityAlias.is_deleted == False,
                ).all()
            ]
            count = session.query(ExtractedEntity).filter(
                ExtractedEntity.dict_id == ent.id,
                ExtractedEntity.is_deleted == False,
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


def merge_entities(
    keeper_dict_id: int,
    absorbed_dict_ids: list[int],
    field_decisions: dict,
    reviewer: str = "",
    note: str = "",
) -> dict:
    """
    实体身份合并（事务内）：
    1. 快照 before_snapshot
    2. 后端计算 final_canonical_data（auto-merge + field_decisions）
    3. keeper.canonical_data = final_canonical_data
    4. absorbed 的 extracted_entities / aliases → keeper
    5. absorbed entity_dict 软删除
    6. absorbed 相关 entity_merge_queue pending → resolved
    7. 写 entity_merge_log + operation_log
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return {"success": False, "error": "数据库未连接"}

        # 校验 keeper
        keeper = session.query(EntityDict).filter(
            EntityDict.id == keeper_dict_id,
            EntityDict.is_deleted == False,
        ).first()
        if keeper is None:
            return {"success": False, "error": f"keeper dict_id={keeper_dict_id} 不存在"}

        # 校验 absorbed
        absorbed_entities = session.query(EntityDict).filter(
            EntityDict.id.in_(absorbed_dict_ids),
            EntityDict.is_deleted == False,
        ).all()
        if len(absorbed_entities) != len(absorbed_dict_ids):
            return {"success": False, "error": "部分 absorbed dict_id 不存在或已删除"}

        # ── 1. 快照 before_snapshot ─────────────────────────────
        keeper_cd = dict(keeper.canonical_data or {})
        before = {
            "keeper": {
                "dict_id": keeper.id,
                "entity_type": keeper.entity_type,
                "entity_name": keeper_cd.get("name", ""),
                "canonical_data": keeper_cd,
                "canonical_source": keeper.canonical_source or "",
                "is_verified": keeper.is_verified,
            },
            "absorbed": [],
        }

        for abs_ent in absorbed_entities:
            abs_cd = dict(abs_ent.canonical_data or {})
            abs_aliases = [
                {"id": a.id, "name": a.name, "is_canonical": a.is_canonical}
                for a in session.query(EntityAlias).filter(
                    EntityAlias.dict_id == abs_ent.id,
                    EntityAlias.is_deleted == False,
                ).all()
            ]
            abs_extractions = session.query(ExtractedEntity).filter(
                ExtractedEntity.dict_id == abs_ent.id,
                ExtractedEntity.is_deleted == False,
            ).all()

            before["absorbed"].append({
                "dict_id": abs_ent.id,
                "entity_type": abs_ent.entity_type,
                "entity_name": abs_cd.get("name", ""),
                "canonical_data": abs_cd,
                "canonical_source": abs_ent.canonical_source or "",
                "is_verified": abs_ent.is_verified,
                "aliases": [{"id": a["id"], "name": a["name"]} for a in abs_aliases],
                "extracted_entity_ids": [e.id for e in abs_extractions],
            })

        # ── 2. 计算合并后的 canonical_data ─────────────────────
        entity_type = keeper.entity_type
        valid_fields = _VALID_FIELDS_BY_TYPE.get(entity_type)
        skip_fields = {"name", "name_en", "aliases", "source_text"}

        keeper_cd = dict(keeper.canonical_data or {})
        merged_cd = dict(keeper_cd)

        for abs_ent in absorbed_entities:
            abs_cd = dict(abs_ent.canonical_data or {})
            for key, val in abs_cd.items():
                if key.startswith("_") or key in skip_fields:
                    continue
                if valid_fields is not None and key not in valid_fields:
                    continue

                keeper_val = merged_cd.get(key)
                if keeper_val is None:
                    # keeper 无 → 补入 absorbed
                    merged_cd[key] = val
                elif keeper_val != val:
                    # 都有但不同 → 看 field_decisions
                    decision = field_decisions.get(key)
                    if decision and decision.get("picked_dict_id"):
                        picked_id = decision["picked_dict_id"]
                        if picked_id == abs_ent.id:
                            merged_cd[key] = val
                        # 否则保留 keeper 的值（不覆盖）

        keeper.canonical_data = merged_cd
        keeper.canonical_updated_at = datetime.now(timezone.utc)

        # ── 3a. 迁移 extracted_entities ─────────────────────────
        migrated_extractions = 0
        for abs_ent in absorbed_entities:
            exts = session.query(ExtractedEntity).filter(
                ExtractedEntity.dict_id == abs_ent.id,
                ExtractedEntity.is_deleted == False,
            ).all()
            for ext in exts:
                ext.dict_id = keeper_dict_id
                migrated_extractions += 1

        # ── 3b. 迁移 aliases（同名去重） ─────────────────────────
        existing_alias_names = {
            a.name.lower()
            for a in session.query(EntityAlias).filter(
                EntityAlias.dict_id == keeper_dict_id,
                EntityAlias.is_deleted == False,
            ).all()
        }
        migrated_aliases = 0
        for abs_ent in absorbed_entities:
            abs_aliases = session.query(EntityAlias).filter(
                EntityAlias.dict_id == abs_ent.id,
                EntityAlias.is_deleted == False,
            ).all()
            for alias in abs_aliases:
                if alias.name.lower() in existing_alias_names:
                    alias.is_deleted = True  # 同名 → 软删除
                else:
                    alias.dict_id = keeper_dict_id
                    existing_alias_names.add(alias.name.lower())
                    migrated_aliases += 1

        # 软删除 absorbed entity_dict
        for abs_ent in absorbed_entities:
            abs_ent.is_deleted = True

        # ── 5. 解决 absorbed 相关的 pending merge_queue ─────────
        resolved_queue = 0
        for abs_ent in absorbed_entities:
            pending_items = session.query(EntityMergeQueue).filter(
                EntityMergeQueue.target_dict_id == abs_ent.id,
                EntityMergeQueue.resolution == "pending",
            ).all()
            for item in pending_items:
                item.resolution = "resolved_merged"
                item.reviewed_by = reviewer
                item.reviewed_at = datetime.now(timezone.utc)
                resolved_queue += 1

        # ── 6a. 写 entity_merge_log ─────────────────────────────
        after = {
            "keeper_dict_id": keeper_dict_id,
            "canonical_data": merged_cd,
            "absorbed_dict_ids": absorbed_dict_ids,
        }
        merge_log = EntityMergeLog(
            keeper_dict_id=keeper_dict_id,
            absorbed_dict_ids=absorbed_dict_ids,
            before_snapshot=before,
            after_snapshot=after,
            field_decisions=field_decisions,
            merge_type="identity",
            reviewer=reviewer,
            note=note,
        )
        session.add(merge_log)
        session.flush()
        merge_log_id = merge_log.id

        # ── 6b. 写 operation_log ────────────────────────────────
        _write_operation_log(
            session, "update", "entity_dict", keeper_dict_id,
            business_action="identity_merge",
            description=f"实体身份合并: keeper={keeper_dict_id} absorbed={absorbed_dict_ids}",
            details={
                "keeper_dict_id": keeper_dict_id,
                "absorbed_dict_ids": absorbed_dict_ids,
                "field_decisions": field_decisions,
                "migrated_extractions": migrated_extractions,
                "migrated_aliases": migrated_aliases,
                "merge_log_id": merge_log_id,
            },
            operator=reviewer,
            trigger_source="merge_entities",
        )

        session.commit()

        logger.info(
            f"[DB] merge_entities: keeper={keeper_dict_id} "
            f"absorbed={absorbed_dict_ids} "
            f"extractions={migrated_extractions} aliases={migrated_aliases} "
            f"merge_log={merge_log_id}"
        )

        return {
            "success": True,
            "merge_log_id": merge_log_id,
            "keeper_dict_id": keeper_dict_id,
            "merged_count": len(absorbed_dict_ids),
            "migrated_extractions": migrated_extractions,
            "migrated_aliases": migrated_aliases,
            "migrated_relations": 0,
        }


def rollback_merge(merge_log_id: int, reason: str = "") -> dict:
    """回滚实体合并：从 entity_merge_log.before_snapshot 恢复状态。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return {"success": False, "error": "数据库未连接"}

        merge_log = session.query(EntityMergeLog).filter(
            EntityMergeLog.id == merge_log_id
        ).first()
        if merge_log is None:
            return {"success": False, "error": f"merge_log_id={merge_log_id} 不存在"}
        if merge_log.is_rolled_back:
            return {"success": False, "error": "该合并记录已被回滚"}

        before = merge_log.before_snapshot or {}
        keeper_snap = before.get("keeper", {})
        absorbed_snaps = before.get("absorbed", [])

        # ── 1. 恢复 keeper canonical_data ───────────────────────
        keeper = session.query(EntityDict).filter(
            EntityDict.id == keeper_snap.get("dict_id"),
            EntityDict.is_deleted == False,
        ).first()
        if keeper:
            keeper.canonical_data = keeper_snap.get("canonical_data", {})
            keeper.canonical_source = keeper_snap.get("canonical_source", "")
            keeper.is_verified = keeper_snap.get("is_verified", False)

        restored_dict_ids: list[int] = []
        restored_extractions = 0
        restored_aliases = 0

        # ── 2. 恢复每个 absorbed entity_dict ───────────────────
        for abs_snap in absorbed_snaps:
            abs_dict_id = abs_snap.get("dict_id")
            abs_ent = session.query(EntityDict).filter(
                EntityDict.id == abs_dict_id,
            ).first()
            if abs_ent is None:
                continue
            abs_ent.is_deleted = False
            abs_ent.canonical_data = abs_snap.get("canonical_data", {})
            abs_ent.canonical_source = abs_snap.get("canonical_source", "")
            abs_ent.is_verified = abs_snap.get("is_verified", False)
            restored_dict_ids.append(abs_dict_id)

            # 恢复 extracted_entities.dict_id
            for ext_id in abs_snap.get("extracted_entity_ids", []):
                ext = session.query(ExtractedEntity).filter(
                    ExtractedEntity.id == ext_id,
                ).first()
                if ext and ext.dict_id != abs_dict_id:
                    ext.dict_id = abs_dict_id
                    restored_extractions += 1

            # 恢复 aliases（包括被同名去重软删除的）
            for alias_info in abs_snap.get("aliases", []):
                alias = session.query(EntityAlias).filter(
                    EntityAlias.id == alias_info.get("id"),
                ).first()
                if alias:
                    if alias.is_deleted:
                        alias.is_deleted = False
                        restored_aliases += 1
                    if alias.dict_id != abs_dict_id:
                        alias.dict_id = abs_dict_id
                        restored_aliases += 1

        # ── 3. 标记回滚 ─────────────────────────────────────────
        merge_log.is_rolled_back = True
        merge_log.rolled_back_at = datetime.now(timezone.utc)
        merge_log.rollback_reason = reason

        _write_operation_log(
            session, "update", "entity_merge_log", merge_log_id,
            business_action="rollback_merge",
            description=f"回滚实体合并: merge_log_id={merge_log_id} reason={reason}",
            details={
                "merge_log_id": merge_log_id,
                "reason": reason,
                "restored_dict_ids": restored_dict_ids,
            },
            trigger_source="rollback_merge",
        )

        session.commit()

        logger.info(
            f"[DB] rollback_merge: merge_log_id={merge_log_id} "
            f"restored={restored_dict_ids} "
            f"extractions={restored_extractions} aliases={restored_aliases}"
        )

        return {
            "success": True,
            "merge_log_id": merge_log_id,
            "restored_dict_ids": restored_dict_ids,
            "restored_extractions": restored_extractions,
            "restored_aliases": restored_aliases,
            "restored_relations": 0,
        }


def get_merge_logs(limit: int = 50, include_rolled_back: bool = True) -> list[dict]:
    """查询合并日志列表。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []

        q = session.query(EntityMergeLog).order_by(EntityMergeLog.created_at.desc())
        if not include_rolled_back:
            q = q.filter(EntityMergeLog.is_rolled_back == False)
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
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


# ═══════════════════════════════════════════════════════════════════════
# 4. EntityAlias Repository（消歧核心）
# ═══════════════════════════════════════════════════════════════════════

def lookup_alias(name: str, entity_type: str, session=None) -> Optional[int]:
    """
    精确匹配别名表。
    在 entity_aliases 中查找 name 完全匹配（同 entity_type）。
    命中返回 dict_id，未命中返回 None。
    session 可选：传入则复用，不传则自开（向后兼容）。
    """
    def _query(sess):
        result = (
            sess.query(EntityAlias.dict_id)
            .filter(
                func.lower(EntityAlias.name) == name.lower(),
                EntityAlias.entity_type == entity_type,
                EntityAlias.is_deleted == False,
            )
            .first()
        )
        return result.dict_id if result else None

    if session is not None:
        return _query(session)

    from db.session import get_session
    with get_session() as sess:
        if sess is None:
            return None
        return _query(sess)


def search_similar_entities(
    embedding: list[float],
    entity_type: str,
    top_k: int = 10,
    session=None,
) -> list[dict]:
    """
    向量相似度召回（pgvector，仅做候选召回，分数供参考不直接决策）。
    在 entity_aliases.name_embedding 上做余弦近邻搜索。
    返回 top_k 条，每条包含：dict_id, name, similarity_score, canonical_data, aliases
    session 可选：传入则复用，不传则自开（向后兼容）。
    """
    def _query(sess):
        try:
            dist = EntityAlias.name_embedding.cosine_distance(embedding)
            rows = (
                sess.query(
                    EntityAlias.dict_id,
                    EntityAlias.name,
                    (1 - dist).label("similarity_score"),
                    EntityDict.canonical_data,
                )
                .join(EntityDict, EntityAlias.dict_id == EntityDict.id)
                .filter(
                    EntityAlias.is_deleted == False,
                    EntityDict.is_deleted == False,
                    EntityDict.entity_type == entity_type,
                    EntityAlias.name_embedding.isnot(None),
                )
                .order_by(dist)
                .limit(top_k)
                .all()
            )
            all_dict_ids = [r.dict_id for r in rows]
            alias_rows = (
                sess.query(EntityAlias.dict_id, EntityAlias.name)
                .filter(
                    EntityAlias.dict_id.in_(all_dict_ids),
                    EntityAlias.is_deleted == False,
                )
                .all()
            )
            aliases_by_dict: dict[int, list[str]] = {}
            for did, name in alias_rows:
                aliases_by_dict.setdefault(did, []).append(name)

            results = []
            for r in rows:
                canonical = r.canonical_data or {}
                results.append({
                    "dict_id": r.dict_id,
                    "name": r.name,
                    "similarity_score": float(r.similarity_score),
                    "canonical_data": canonical,
                    "aliases": aliases_by_dict.get(r.dict_id, []),
                })
            return results
        except Exception as e:
            logger.error(f"[DB] search_similar_entities 失败: {e}")
            return []

    if session is not None:
        return _query(session)

    from db.session import get_session
    with get_session() as sess:
        if sess is None:
            return []
        return _query(sess)


def add_aliases(dict_id: int, entity_type: str, aliases: list[tuple[str, str, bool]]) -> None:
    """
    添加别名（幂等，已存在则跳过）。
    aliases: [(name, lang, is_canonical), ...]
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        for name, lang, is_canonical in aliases:
            exists = (
                session.query(EntityAlias)
                .filter(EntityAlias.dict_id == dict_id, EntityAlias.name == name,
                        EntityAlias.is_deleted == False)
                .first()
            )
            if exists is None:
                alias = EntityAlias(
                    dict_id=dict_id,
                    name=name,
                    lang=lang or "",
                    is_canonical=bool(is_canonical),
                    entity_type=entity_type,
                )
                session.add(alias)
                session.flush()
                _write_operation_log(
                    session, "insert", "entity_aliases", alias.id,
                    business_action="alias_sync",
                    description=f"添加别名: '{name}' dict_id={dict_id}",
                    details={"dict_id": dict_id, "name": name, "lang": lang, "is_canonical": bool(is_canonical)},
                    trigger_source="add_aliases",
                )


def lookup_external_id(
    entity_type: str,
    external_key: str,
    external_value: str,
    session=None,
) -> dict | None:
    """
    在 entity_dict.external_ids JSONB 中查找匹配的外部 ID。
    external_ids 的结构如 {"stock_code": "000001", "cas_number": "50-78-2"}。
    命中返回 {"dict_id": int, "canonical_name": str}，未命中返回 None。
    session 可选：传入则复用，不传则自开（向后兼容）。
    """
    def _query(sess):
        row = sess.query(
            EntityDict.id,
            EntityDict.canonical_data,
        ).filter(
            EntityDict.entity_type == entity_type,
            EntityDict.is_deleted == False,
            text("entity_dict.external_ids ->> :key = :val"),
        ).params(key=external_key, val=str(external_value).strip()).first()
        if row:
            canonical = row.canonical_data or {}
            return {"dict_id": row.id, "canonical_name": canonical.get("name", "")}
        return None

    if session is not None:
        return _query(session)

    from db.session import get_session
    with get_session() as sess:
        if sess is None:
            return None
        return _query(sess)


def get_entity_with_aliases(dict_id: int) -> dict | None:
    """获取 entity_dict 记录及所有 alias 名称，供 LLM 精排使用。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return None
        entity = session.query(EntityDict).filter(
            EntityDict.id == dict_id, EntityDict.is_deleted == False
        ).first()
        if not entity:
            return None
        aliases = session.query(EntityAlias.name).filter(
            EntityAlias.dict_id == dict_id,
            EntityAlias.is_deleted == False,
        ).all()
        return {
            "dict_id": entity.id,
            "entity_type": entity.entity_type,
            "canonical_data": entity.canonical_data or {},
            "aliases": [a.name for a in aliases],
        }


def add_alias_with_embedding(
    dict_id: int,
    name: str,
    entity_type: str = "",
    lang: str = "",
    is_canonical: bool = False,
    embedding: list[float] | None = None,
) -> None:
    """
    添加别名并附带 embedding 向量。
    别名已存在则跳过。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        exists = session.query(EntityAlias).filter(
            EntityAlias.dict_id == dict_id, EntityAlias.name == name,
            EntityAlias.is_deleted == False,
        ).first()
        if exists is not None:
            return

        alias = EntityAlias(
            dict_id=dict_id,
            name=name,
            lang=lang or "",
            is_canonical=bool(is_canonical),
            entity_type=entity_type,
            name_embedding=embedding,
        )
        session.add(alias)
        session.flush()

        _write_operation_log(
            session, "insert", "entity_aliases", alias.id,
            business_action="alias_sync",
            description=f"添加别名(含embedding): '{name}' dict_id={dict_id}",
            details={"dict_id": dict_id, "name": name, "lang": lang, "is_canonical": bool(is_canonical),
                      "has_embedding": bool(embedding)},
            trigger_source="add_alias_with_embedding",
        )


# ── 提取结果写入前：实体合并辅助函数 ─────────────────────────────────

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


# ═══════════════════════════════════════════════════════════════════════
# 5. 抽取结果写入（核心：实体 + 关系 + 状态更新，同一事务）
# ═══════════════════════════════════════════════════════════════════════

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


def _do_insert_extraction_result(
    session,
    source_doc_uuid: str,
    source_site: str,
    entities: list[dict],
    relations: list[dict],
) -> list[int]:
    """insert_extraction_result 的核心逻辑，在已有 session 中执行。"""
    n = len(entities)

    # ── Phase 1: 分类实体 ─────────────────────────────────────────
    # merged_id_map: 输入下标 → 已有 extracted_entities.id
    # insert_indices: 需要插入的实体下标列表
    #
    # dict_id 只由 Step 2 别名精确匹配设定，安全可靠，直接合并。
    # Step 1/3/5 不会设 dict_id，走 Phase 2 创建新 entity_dict + alias。
    merged_id_map: dict[int, int] = {}
    insert_indices: list[int] = []

    for i, rec in enumerate(entities):
        dict_id = rec.get("dict_id")
        entity_type = rec.get("entity_type", "")
        entity_name = rec.get("entity_name", "")

        if dict_id:
            existing = _find_existing_entity_by_dict_id(session, dict_id, entity_type)
            if existing is not None:
                merged_id_map[i] = existing.id
                logger.info(
                    f"[DB] Phase1 MERGE: '{entity_name}' type={entity_type} "
                    f"dict_id={dict_id} → existing extracted_entities.id={existing.id}"
                )
                continue
            else:
                logger.warning(
                    f"[DB] Phase1 INSERT (dict_id有值但无已存在行): "
                    f"'{entity_name}' type={entity_type} dict_id={dict_id}"
                )

        insert_indices.append(i)

    logger.info(
        f"[DB] 实体分类: doc={source_doc_uuid} "
        f"total={n} merge={len(merged_id_map)} insert={len(insert_indices)}"
    )

    # ── Phase 2: 插入新实体 ───────────────────────────────────────
    # new_entity → 自动创建 entity_dict（is_verified=False），让后续消歧可匹配
    # batch_dict_cache 防止同一批次内重复创建同名实体
    batch_dict_cache: dict[tuple[str, str], int] = {}

    new_entities: list[ExtractedEntity] = []
    new_index_to_obj: dict[int, ExtractedEntity] = {}

    for i in insert_indices:
        rec = entities[i]
        entity_type = rec["entity_type"]
        entity_name = (rec.get("entity_name") or "").strip()
        entity_data = rec.get("entity_data", {}) or {}
        disambig = entity_data.get("_disambig", {}) or {}
        dict_id = rec.get("dict_id")

        # dict_id 仅由 Step 2 别名精确匹配设定，其余一律创建新 entity_dict + alias
        if dict_id is None and entity_name:
            cache_key = (entity_type, entity_name)
            if cache_key in batch_dict_cache:
                dict_id = batch_dict_cache[cache_key]
                logger.info(
                    f"[DB] Phase2 batch_cache HIT: '{entity_name}' type={entity_type} "
                    f"dict_id={dict_id} (批次内复用)"
                )
            else:
                new_dict_id = _create_entity_dict_from_extraction(
                    session, entity_name, entity_type, entity_data,
                    source_site=source_site, is_verified=False,
                    source_doc_uuid=source_doc_uuid,
                )
                if new_dict_id:
                    dict_id = new_dict_id
                    batch_dict_cache[cache_key] = new_dict_id
            rec["dict_id"] = dict_id

        ent = ExtractedEntity(
            source_doc_uuid=source_doc_uuid,
            source_site=source_site,
            entity_type=entity_type,
            entity_name=rec.get("entity_name", ""),
            entity_data=entity_data,
            source_text=rec.get("source_text", ""),
            dict_id=rec.get("dict_id"),
            confidence_score=rec.get("confidence_score", 0.0),
            confidence_reason=rec.get("confidence_reason", ""),
            review_status="pending",
        )
        new_entities.append(ent)
        new_index_to_obj[i] = ent

    if new_entities:
        session.add_all(new_entities)
        session.flush()  # 获取自增 id
        for ent_obj in new_entities:
            _write_operation_log(
                session, "insert", "extracted_entities", ent_obj.id,
                business_action="extract",
                description=f"新实体: name='{ent_obj.entity_name}' type={ent_obj.entity_type} dict_id={ent_obj.dict_id}",
                details={"entity_name": ent_obj.entity_name, "entity_type": ent_obj.entity_type,
                          "dict_id": ent_obj.dict_id, "source_doc_uuid": ent_obj.source_doc_uuid,
                          "source_site": ent_obj.source_site, "confidence": ent_obj.confidence_score},
                trigger_source="_do_insert_extraction_result",
            )

    # final_id_map: 输入下标 → DB id
    final_id_map: dict[int, int] = {}
    for i in merged_id_map:
        final_id_map[i] = merged_id_map[i]
    for i in insert_indices:
        ent_obj = new_index_to_obj[i]
        final_id_map[i] = ent_obj.id

    # ── Phase 3: 合并数据到已有实体 ───────────────────────────────
    merge_count = 0
    merged_dict_ids: set[int] = set()
    for i, existing_id in merged_id_map.items():
        existing_ent = session.query(ExtractedEntity).filter(
            ExtractedEntity.id == existing_id
        ).first()
        if existing_ent is None:
            logger.warning(f"[DB] merge 目标实体不存在: id={existing_id}")
            continue

        rec = entities[i]
        dict_id = rec.get("dict_id")
        entity_name = rec.get("entity_name", "")
        entity_type = rec.get("entity_type", "")
        new_entity_data = rec.get("entity_data", {}) or {}

        # 合并 entity_data → process_entity_merge 写入 entity_dict.canonical_data
        merge_auto, merge_conflict = process_entity_merge(
            session, dict_id, existing_id, new_entity_data, source_site,
        )

        # 交叉验证：第二次被命中 → 实体自动通过
        if existing_ent.review_status == "pending":
            existing_ent.review_status = "auto_approved"
            logger.info(
                f"[DB] auto_approve: entity_id={existing_id} dict_id={dict_id} "
                f"'{existing_ent.entity_name}' 交叉验证自动通过"
            )

        # 重置图同步状态，让更新推送到 WeKnora/Neo4j
        if existing_ent.weknora_graph_status == "pushed":
            existing_ent.weknora_graph_status = "pending"

        # 合并 source_text 用于溯源
        new_source = rec.get("source_text", "")
        if new_source and new_source not in (existing_ent.source_text or ""):
            existing_ent.source_text = (
                (existing_ent.source_text or "")
                + "\n---\n"
                + new_source
            )[:2000]

        merge_count += 1
        if dict_id:
            merged_dict_ids.add(dict_id)
        logger.info(
            f"[DB] 实体合并: index={i} name='{entity_name}' "
            f"→ extracted_entities.id={existing_id} dict_id={dict_id}"
        )
        _write_operation_log(
            session, "update", "extracted_entities", existing_id,
            business_action="merge",
            description=f"merge 实体: name='{entity_name}' type={entity_type} → id={existing_id} dict_id={dict_id}",
            details={"entity_name": entity_name, "entity_type": entity_type,
                      "dict_id": dict_id, "merge_auto": merge_auto, "merge_conflict": merge_conflict,
                      "source_site": source_site},
            trigger_source="_do_insert_extraction_result",
        )

    if merge_count:
        logger.info(
            f"[DB] merge 阶段完成: {merge_count} 个实体"
        )
        # 统一回写 canonical_data → entity_data（仅 approved/auto_approved）
        for _dict_id in merged_dict_ids:
            _sync_canonical_to_extracted(session, _dict_id)

    # ── Phase 4: 写关系（下标 → final_id_map）─────────────────────
    # 查询当前文档已有关系，去重
    existing_relations = set()
    if relations:
        for er in (
            session.query(
                EntityRelation.from_entity_id,
                EntityRelation.to_entity_id,
                EntityRelation.relation_type,
            )
            .filter(
                EntityRelation.source_doc_uuid == source_doc_uuid,
                EntityRelation.is_deleted == False,
            )
            .all()
        ):
            existing_relations.add((er.from_entity_id, er.to_entity_id, er.relation_type))

    rel_objs = []
    skipped_dups = 0
    for rel in relations:
        from_idx = rel.get("from_entity_idx")
        to_idx = rel.get("to_entity_idx")

        if from_idx is None or to_idx is None:
            continue
        if from_idx >= n or to_idx >= n:
            logger.warning(
                f"[DB] insert_extraction_result: 关系下标越界 "
                f"from={from_idx} to={to_idx} n={n}"
            )
            continue

        from_db_id = final_id_map.get(from_idx)
        to_db_id = final_id_map.get(to_idx)
        if from_db_id is None or to_db_id is None:
            logger.warning(
                f"[DB] insert_extraction_result: 关系下标无对应 DB id "
                f"from={from_idx}({from_db_id}) to={to_idx}({to_db_id})"
            )
            continue

        # 去重：同文档同 (from, to, type) 已存在则跳过
        dup_key = (from_db_id, to_db_id, rel["relation_type"])
        if dup_key in existing_relations:
            skipped_dups += 1
            continue

        existing_relations.add(dup_key)  # 防止批次内重复
        rel_objs.append(EntityRelation(
            source_doc_uuid=source_doc_uuid,
            source_site=source_site,
            from_entity_id=from_db_id,
            to_entity_id=to_db_id,
            relation_type=rel["relation_type"],
            relation_props=rel.get("relation_props", {}),
            source_text=rel.get("source_text", ""),
            confidence_score=rel.get("confidence_score", 0.0),
            confidence_reason=rel.get("confidence_reason", ""),
            review_status="pending",
        ))
    if rel_objs:
        session.add_all(rel_objs)
        session.flush()
        for rel_obj in rel_objs:
            _write_operation_log(
                session, "insert", "entity_relations", rel_obj.id,
                business_action="extract",
                description=f"新关系: {rel_obj.relation_type} from={rel_obj.from_entity_id}→to={rel_obj.to_entity_id}",
                details={"relation_type": rel_obj.relation_type,
                          "from_entity_id": rel_obj.from_entity_id, "to_entity_id": rel_obj.to_entity_id,
                          "source_doc_uuid": rel_obj.source_doc_uuid, "source_site": rel_obj.source_site},
                trigger_source="_do_insert_extraction_result",
            )

    if skipped_dups:
        logger.info(
            f"[DB] 关系去重: 跳过 {skipped_dups} 条重复关系 "
            f"(doc={source_doc_uuid})"
        )

    # ── Phase 5: 更新文档抽取状态 ──────────────────────────────────
    doc = session.query(Document).filter(Document.uuid == source_doc_uuid).first()
    if doc:
        doc.extraction_status = "done"
        doc.extracted_at = datetime.now(timezone.utc)

    from db.review_lifecycle import recalc_document_state
    recalc_document_state(session, source_doc_uuid)

    final_ids = [final_id_map.get(i) for i in range(n)]
    logger.info(
        f"[DB] 抽取结果入库: doc={source_doc_uuid} "
        f"entities={n} (new={len(insert_indices)} merged={len(merged_id_map)}) "
        f"relations={len(relations)}"
    )
    return final_ids


# ═══════════════════════════════════════════════════════════════════════
# 6. 审核队列接口（实体）
# ═══════════════════════════════════════════════════════════════════════

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


def approve_entity(id: int, reviewer: str, note: str = "") -> None:
    """审核通过实体，同时写 review_log。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        ent = session.query(ExtractedEntity).filter(ExtractedEntity.id == id).first()
        if ent is None:
            logger.warning(f"[DB] approve_entity: 实体 {id} 不存在")
            return
        before = ent.review_status
        ent.review_status = "approved"
        ent.reviewer_note = note
        ent.reviewed_by = reviewer
        ent.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        _write_review_log(session, "entity", id, "approve", reviewer, note,
                          {"review_status": before}, {"review_status": "approved"})
        _write_operation_log(
            session, "update", "extracted_entities", id,
            business_action="approve",
            description=f"审核通过(简单): name='{ent.entity_name}' type={ent.entity_type}",
            details={"review_status": {"before": before, "after": "approved"}, "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="approve_entity",
        )
        from db.review_lifecycle import recalc_document_state
        recalc_document_state(session, ent.source_doc_uuid)


def _extract_external_ids(entity_type: str, entity_data: dict) -> dict:
    """从 entity_data 中提取 key → value 映射，供 entity_dict.external_ids 使用。"""
    FIELD_MAP: dict[str, list[str]] = {
        "Company":       ["id", "stock_code"],
        "Drug":          ["id", "cas_number"],
        "Target":        ["id", "gene_symbol"],
        "ClinicalTrial": ["trial_id"],
        "ApprovalEvent": ["approval_number"],
        "Compound":      ["cas_number"],
        "Policy":        ["doc_number"],
        "Pathway":       ["id"],
        "Indication":    ["id", "icd_code"],
    }
    result: dict[str, str] = {}
    for f in FIELD_MAP.get(entity_type, []):
        val = entity_data.get(f)
        if val and str(val).strip():
            result[f] = str(val).strip()
    return result


def _create_entity_dict_from_extraction(
    session,
    entity_name: str,
    entity_type: str,
    entity_data: dict,
    source_site: str = "",
    is_verified: bool = True,
    source_doc_uuid: str = "",
) -> int | None:
    """
    从抽取实体创建 entity_dict 记录，返回 dict_id。
    已存在同 entity_type + 同 canonical_name 则跳过（返回已有 id）。
    is_verified=False 用于首次抽取自动创建（未经人工审核）。
    """
    # 跨进程漏斗锁：同 entity_type + entity_name 的创建操作在 DB 层面串行化。
    # 事务提交时 pg_advisory_xact_lock 自动释放，不需要手动 unlock。
    lock_key = hash((entity_type, entity_name.strip().lower())) & 0x7FFFFFFF
    logger.debug(
        f"[DB] 获取 advisory lock: name='{entity_name}' type={entity_type} "
        f"key={lock_key}"
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    existing = (
        session.query(EntityDict.id)
        .join(EntityAlias, EntityAlias.dict_id == EntityDict.id)
        .filter(
            func.lower(EntityAlias.name) == entity_name.lower(),
            EntityDict.entity_type == entity_type,
            EntityDict.is_deleted == False,
            EntityAlias.is_deleted == False,
        )
        .first()
    )
    if existing:
        logger.info(f"[DB] entity_dict 已存在: name='{entity_name}' type={entity_type} dict_id={existing.id}，跳过创建")
        return existing.id

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
        is_verified=is_verified,
    )
    session.add(entity)
    session.flush()
    _write_operation_log(
        session, "insert", "entity_dict", entity.id,
        business_action="extract",
        description=f"新建 entity_dict: name='{entity_name}' type={entity_type} source={source_site}",
        details={"entity_name": entity_name, "entity_type": entity_type, "canonical_data": canonical_data, "is_verified": is_verified},
        trigger_source="_create_entity_dict_from_extraction",
    )
    # 写入 alias（embedding 已在锁外预生成，从 entity_data._embeddings 缓存读取）
    _write_aliases_with_embedding(session, entity.id, entity_type, entity_name, entity_data,
                                  source_doc_uuid=source_doc_uuid)
    logger.info(f"[DB] entity_dict 创建成功: dict_id={entity.id}")
    return entity.id


def _write_aliases_with_embedding(
    session,
    dict_id: int,
    entity_type: str,
    entity_name: str,
    entity_data: dict,
    source_doc_uuid: str = "",
) -> int:
    """将 entity_name + entity_data 中的名称写为 entity_aliases，附带 embedding。返回新增条数。"""
    from core_engine.cloud_api_client import generate_embedding

    names_to_add: set[str] = set()
    names_to_add.add(entity_name.strip())

    data_name = entity_data.get("name", "").strip()
    if data_name:
        names_to_add.add(data_name)
    data_name_en = entity_data.get("name_en", "").strip()
    if data_name_en:
        names_to_add.add(data_name_en)
    for alias in (entity_data.get("aliases") or []):
        alias_str = str(alias).strip()
        if alias_str:
            names_to_add.add(alias_str)

    # 锁外预生成的 embedding 缓存，优先使用，未命中则实时调 API
    embeddings_cache: dict[str, list[float]] = entity_data.pop("_embeddings", {}) or {}

    logger.info(f"[DB] alias write-back: dict_id={dict_id} names={list(names_to_add)}")

    added = 0
    for name in names_to_add:
        if not name:
            continue
        # 制剂后缀实体：不写入 entity_name 的子串作为 alias（如 "氯雷他定片" ← "氯雷他定"）
        if (_FORMULATION_SUFFIX.search(entity_name)
                and name != entity_name and name in entity_name):
            logger.info(f"[DB] alias '{name}' 是 entity_name '{entity_name}' 的子串，跳过")
            continue
        exists = (
            session.query(EntityAlias)
            .filter(EntityAlias.dict_id == dict_id, EntityAlias.name == name,
                    EntityAlias.is_deleted == False)
            .first()
        )
        if exists is not None:
            logger.debug(f"[DB] alias 已存在: '{name}' dict_id={dict_id}，跳过")
            continue

        # 跨 dict_id 冲突检查：该 name 已属于其他实体则跳过
        conflict = session.query(EntityAlias).filter(
            EntityAlias.dict_id != dict_id,
            EntityAlias.name == name,
            EntityAlias.entity_type == entity_type,
            EntityAlias.is_deleted == False,
        ).first()
        if conflict is not None:
            logger.info(
                f"[DB] alias '{name}' 已属于 dict_id={conflict.dict_id}"
                f" (entity_type={entity_type})，跳过不写入 dict_id={dict_id}"
            )
            continue

        try:
            embedding = embeddings_cache.get(name) or generate_embedding(name)
        except Exception as e:
            logger.warning(f"[DB] alias '{name}' embedding 生成失败: {e}")
            embedding = None

        alias = EntityAlias(
            dict_id=dict_id,
            name=name,
            lang="zh" if any('一' <= c <= '鿟' for c in name) else "en",
            is_canonical=(name == entity_data.get("name", "")),
            entity_type=entity_type,
            name_embedding=embedding,
        )
        session.add(alias)
        session.flush()

        if embedding:
            logger.debug(f"[DB] alias '{name}' written with embedding dims={len(embedding)}")
        else:
            logger.warning(f"[DB] alias '{name}' embedding 生成失败")
        added += 1

    if added and source_doc_uuid:
        names_written = [n for n in names_to_add if n]  # 所有写入的候选名
        _write_operation_log(
            session, "insert", "entity_aliases", None,
            business_action="alias_sync",
            description=f"LLM抽取别名: entity='{entity_name}' type={entity_type} dict_id={dict_id} aliases={names_written}",
            details={
                "source_doc_uuid": source_doc_uuid,
                "entity_name": entity_name,
                "entity_type": entity_type,
                "dict_id": dict_id,
                "aliases_written": names_written,
            },
            trigger_source="_write_aliases_with_embedding",
        )
    return added


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
        logger.info(
            f"[DB] _sync_canonical_to_extracted: dict_id={dict_id} "
            f"synced {count}/{len(rows)} rows, {len(cd)} canonical fields"
        )

    return count


def approve_entity_with_alias_writeback(
    entity_id: int,
    reviewer: str,
    note: str = "",
    dict_id: int | None = None,
    force_new: bool = False,
) -> None:
    """
    审核通过实体，合并 entity_data 到 canonical_data。
    优先级：force_new > dict_id > ent.dict_id > 创建新 entity_dict
    - force_new=True → 忽略一切，以新实体创建
    - dict_id 不为 None → 审核员指定了目标 dict_id
    - 都不给 → 保留消歧结果（ent.dict_id），无则新建

    约束：已审核实体（review_status='approved'）若已有 dict_id，
    禁止 force_new 或更换 dict_id，保证实体身份不可变。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return

        ent = session.query(ExtractedEntity).filter(ExtractedEntity.id == entity_id).first()
        if ent is None:
            logger.warning(f"[DB] approve_entity_with_alias_writeback: 实体 {entity_id} 不存在")
            return

        # 已审核实体：dict_id 不可变
        if ent.review_status == "approved" and ent.dict_id is not None:
            if force_new:
                logger.warning(
                    f"[DB] approve_entity_with_alias_writeback: 实体 {entity_id} "
                    f"已审核 dict_id={ent.dict_id}，拒绝 force_new"
                )
                return
            if dict_id is not None and dict_id != ent.dict_id:
                logger.warning(
                    f"[DB] approve_entity_with_alias_writeback: 实体 {entity_id} "
                    f"已审核 dict_id={ent.dict_id}，拒绝更换为 {dict_id}"
                )
                return

        before = ent.review_status
        ent.review_status = "approved"
        ent.reviewer_note = note
        ent.reviewed_by = reviewer
        ent.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        _write_review_log(session, "entity", entity_id, "approve", reviewer, note,
                          {"review_status": before}, {"review_status": "approved"})
        _write_operation_log(
            session, "update", "extracted_entities", entity_id,
            business_action="approve",
            description=f"审核通过: name='{ent.entity_name}' type={ent.entity_type} dict_id={ent.dict_id}",
            details={"review_status": {"before": before, "after": "approved"}, "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="approve_entity_with_alias_writeback",
        )
        from db.review_lifecycle import recalc_document_state
        recalc_document_state(session, ent.source_doc_uuid)

        logger.info(
            f"[DB] approve_entity_with_alias_writeback: entity_id={entity_id} "
            f"name='{ent.entity_name}' type={ent.entity_type} dict_id={ent.dict_id} "
            f"override_dict_id={dict_id} force_new={force_new} source_site={ent.source_site}"
        )

        entity_data = ent.entity_data or {}

        # 决定目标 dict_id
        target_dict_id: int | None = None
        if force_new:
            target_dict_id = None  # 强制新建
        elif dict_id is not None:
            target_dict_id = dict_id  # 审核员指定
        else:
            target_dict_id = ent.dict_id  # 保留消歧结果

        if not target_dict_id:
            logger.info(f"[DB] 创建 entity_dict for '{ent.entity_name}'")
            new_id = _create_entity_dict_from_extraction(
                session,
                entity_name=ent.entity_name,
                entity_type=ent.entity_type,
                entity_data=entity_data,
                source_site=ent.source_site or "",
                source_doc_uuid=ent.source_doc_uuid or "",
            )
            if new_id:
                target_dict_id = new_id
            else:
                logger.warning(f"[DB] 无法创建 entity_dict，跳过 alias 回写: entity_id={entity_id}")
                return

        # 更新实体 dict_id（审核员指定或新建）
        old_dict_id = ent.dict_id
        if target_dict_id and target_dict_id != ent.dict_id:
            ent.dict_id = target_dict_id
            session.flush()

        # ── 迁移/清理旧 dict_id 下的别名 ──
        if old_dict_id and target_dict_id and old_dict_id != target_dict_id:
            logger.info(
                f"[DB] approve: dict_id 变更 entity_id={entity_id} "
                f"{old_dict_id} → {target_dict_id}，迁移 alias"
            )
            existing_names = {
                a.name.lower()
                for a in session.query(EntityAlias).filter(
                    EntityAlias.dict_id == target_dict_id,
                    EntityAlias.is_deleted == False,
                ).all()
            }
            old_aliases = session.query(EntityAlias).filter(
                EntityAlias.dict_id == old_dict_id,
                EntityAlias.is_deleted == False,
            ).all()
            for alias in old_aliases:
                if alias.name.lower() in existing_names:
                    alias.is_deleted = True  # 同名 → 软删除
                else:
                    alias.dict_id = target_dict_id  # 迁移到新 dict_id
                    existing_names.add(alias.name.lower())

            # 检查旧 dict_id 是否成孤儿（无其他实体引用）
            remaining = session.query(ExtractedEntity).filter(
                ExtractedEntity.dict_id == old_dict_id,
                ExtractedEntity.is_deleted == False,
            ).count()
            if remaining == 0:
                old_dict = session.query(EntityDict).filter(EntityDict.id == old_dict_id).first()
                if old_dict and not old_dict.is_verified:
                    old_dict.is_deleted = True
                    logger.info(f"[DB] approve: 孤儿 entity_dict {old_dict_id} 已软删除")

            session.flush()

        # 合并 entity_data 到 canonical_data
        merge_auto, merge_conflict = process_entity_merge(
            session, ent.dict_id, entity_id, entity_data, ent.source_site or "",
        )
        if merge_auto or merge_conflict:
            logger.info(
                f"[DB] merge: entity_id={entity_id} dict_id={ent.dict_id} "
                f"auto_merged={merge_auto} conflicts={merge_conflict}"
            )

        # 回写 canonical_data → entity_data（实体刚获批）
        _sync_canonical_to_extracted(session, ent.dict_id)

        # 去重同 dict_id 的其他实体（保留当前审核通过的这一条）
        if ent.dict_id:
            deduped = _dedup_extracted_entities_by_dict_id(session, ent.dict_id, entity_id)
            if deduped:
                logger.info(
                    f"[DB] dedup: entity_id={entity_id} dict_id={ent.dict_id} "
                    f"removed {deduped} duplicates"
                )


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
        
        # 获取所有符合条件的实体
        entities = q.all()
        count = len(entities)
        
        if count == 0:
            return {"success": True, "message": "没有符合条件的待审核实体", "count": 0}
        
        # 批量更新
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
                    session.flush()

            if dict_id:
                process_entity_merge(
                    session, dict_id, ent.id, entity_data, ent.source_site or "",
                )

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


# ═══════════════════════════════════════════════════════════════════════
# 7. 审核队列接口（关系）
# ═══════════════════════════════════════════════════════════════════════

def get_pending_review_relations(limit: int = 50) -> list[dict]:
    """查询 review_status=pending 且 is_deleted=False 的关系。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(EntityRelation)
            .filter(
                EntityRelation.review_status == "pending",
                EntityRelation.is_deleted == False,
            )
            .limit(limit)
            .all()
        )
        return [_relation_to_dict(r) for r in rows]


def approve_relation(id: int, reviewer: str, note: str = "") -> None:
    """审核通过关系，校验两端实体均未被拒绝，写 review_log。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        rel = session.query(EntityRelation).filter(EntityRelation.id == id).first()
        if rel is None:
            logger.warning(f"[DB] approve_relation: 关系 {id} 不存在")
            return

        # 校验两端实体均已通过审核
        endpoint_ids = [rel.from_entity_id, rel.to_entity_id]
        unapproved_entities = (
            session.query(ExtractedEntity.id)
            .filter(
                ExtractedEntity.id.in_(endpoint_ids),
                ExtractedEntity.review_status.notin_(["approved", "auto_approved"]),
                ExtractedEntity.is_deleted == False,
            )
            .all()
        )
        if unapproved_entities:
            unapproved_ids = [r.id for r in unapproved_entities]
            logger.warning(
                f"[DB] approve_relation: 关系 {id} 的端点实体 {unapproved_ids} 尚未通过审核，不允许通过"
            )
            return

        before = rel.review_status
        rel.review_status = "approved"
        rel.reviewer_note = note
        rel.reviewed_by = reviewer
        rel.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        _write_review_log(session, "relation", id, "approve", reviewer, note,
                          {"review_status": before}, {"review_status": "approved"})
        _write_operation_log(
            session, "update", "entity_relations", id,
            business_action="approve",
            description=f"审核通过关系: {rel.relation_type} from={rel.from_entity_id}→to={rel.to_entity_id}",
            details={"relation_type": rel.relation_type,
                      "from_entity_id": rel.from_entity_id, "to_entity_id": rel.to_entity_id,
                      "review_status": {"before": before, "after": "approved"}, "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="approve_relation",
        )


def reject_relation(id: int, reviewer: str, note: str = "") -> None:
    """审核拒绝关系，同时写 review_log。"""
    from db.session import get_session

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


def batch_approve_relations(
    source_doc_uuid: str = "",
    source_site: str = "",
    reviewer: str = "",
    note: str = "",
    min_confidence: float = 0.0
) -> dict:
    """批量审核通过关系，支持按条件筛选。"""
    from db.session import get_session
    
    with get_session() as session:
        if session is None:
            return {"success": False, "message": "数据库未连接", "count": 0}
        
        # 构建查询条件
        q = session.query(EntityRelation).filter(
            EntityRelation.review_status == "pending",
            EntityRelation.is_deleted == False,
            EntityRelation.confidence_score >= min_confidence
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
            rel.review_status = "approved"
            rel.reviewer_note = note
            rel.reviewed_by = reviewer
            rel.reviewed_at = datetime.now(timezone.utc)
            _write_review_log(session, "relation", rel.id, "approve", reviewer, note,
                            {"review_status": before}, {"review_status": "approved"})
        
        session.commit()
        _write_operation_log(
            session, "update", "entity_relations", None,
            business_action="approve",
            description=f"批量审核通过: {count} 个关系",
            details={"count": count,
                      "source_doc_uuid": source_doc_uuid, "source_site": source_site,
                      "reviewer": reviewer, "note": note},
            operator=reviewer,
            trigger_source="batch_approve_relations",
        )
        return {"success": True, "message": f"成功批量通过 {count} 个关系", "count": count}


def batch_reject_relations(
    source_doc_uuid: str = "",
    source_site: str = "",
    reviewer: str = "",
    note: str = ""
) -> dict:
    """批量审核拒绝关系，支持按条件筛选。"""
    from db.session import get_session
    
    with get_session() as session:
        if session is None:
            return {"success": False, "message": "数据库未连接", "count": 0}
        
        # 构建查询条件
        q = session.query(EntityRelation).filter(
            EntityRelation.review_status == "pending",
            EntityRelation.is_deleted == False
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


# ═══════════════════════════════════════════════════════════════════════
# 8. WeKnora 图同步状态接口
# ═══════════════════════════════════════════════════════════════════════

def get_entity_dict_for_weknora(limit: int = 500, offset: int = 0) -> list[dict]:
    """
    查询 weknora_sync_status='pending' 且 is_deleted=False 且 is_verified=True 的 entity_dict 行。
    未审核实体不推送 WeKnora，避免前端出现不完整数据。
    返回字段：id, entity_type, external_ids, canonical_data, canonical_source, is_deleted
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        q = (
            session.query(EntityDict)
            .filter(
                EntityDict.weknora_sync_status == "pending",
                EntityDict.is_deleted == False,
                EntityDict.is_verified == True,
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
    from db.session import get_session

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
    from db.session import get_session

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


# ── entity_dict / entity_alias 软删除（PrimeKG 来源保护）──────────────

def soft_delete_entity_dict(dict_id: int, operator: str = "") -> None:
    """
    软删除 entity_dict 及其所有别名。
    PrimeKG 来源（canonical_source='primekg'）禁止删除。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        ent = session.query(EntityDict).filter(EntityDict.id == dict_id).first()
        if ent is None:
            logger.warning(f"[DB] soft_delete_entity_dict: dict_id={dict_id} 不存在")
            return
        if ent.canonical_source == "primekg":
            logger.warning(
                f"[DB] soft_delete_entity_dict: dict_id={dict_id} 来源为 PrimeKG，禁止删除"
            )
            raise ValueError(f"无法删除 PrimeKG 来源的实体 (dict_id={dict_id})")

        ent.is_deleted = True
        session.query(EntityAlias).filter(
            EntityAlias.dict_id == dict_id,
            EntityAlias.is_deleted == False,
        ).update({"is_deleted": True}, synchronize_session=False)

        _write_operation_log(
            session, "update", "entity_dict", dict_id,
            business_action="delete",
            description=f"软删除 entity_dict: type={ent.entity_type} operator={operator}",
            details={"dict_id": dict_id, "entity_type": ent.entity_type, "canonical_source": ent.canonical_source,
                      "operator": operator},
            operator=operator,
            trigger_source="soft_delete_entity_dict",
        )
        logger.info(
            f"[DB] soft_delete_entity_dict: dict_id={dict_id} "
            f"entity_type={ent.entity_type} operator={operator}"
        )


def soft_delete_entity_alias(alias_id: int, operator: str = "") -> None:
    """
    软删除单个别名。
    若所属 entity_dict 来源为 PrimeKg，禁止删除。
    若别名是 canonical 名称，禁止删除（标准名不可删）。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        alias = session.query(EntityAlias).filter(EntityAlias.id == alias_id).first()
        if alias is None:
            logger.warning(f"[DB] soft_delete_entity_alias: alias_id={alias_id} 不存在")
            return
        if alias.is_deleted:
            return

        parent = session.query(EntityDict).filter(EntityDict.id == alias.dict_id).first()
        if parent is None:
            logger.warning(f"[DB] soft_delete_entity_alias: 所属 entity_dict 不存在 dict_id={alias.dict_id}")
            return
        if parent.canonical_source == "primekg":
            logger.warning(
                f"[DB] soft_delete_entity_alias: alias_id={alias_id} 所属实体来源为 PrimeKG，禁止删除"
            )
            raise ValueError(f"无法删除 PrimeKG 来源实体的别名 (alias_id={alias_id})")

        if alias.is_canonical:
            logger.warning(
                f"[DB] soft_delete_entity_alias: alias_id={alias_id} "
                f"'{alias.name}' 为标准名称，禁止删除"
            )
            raise ValueError(f"无法删除实体标准名称 '{alias.name}' (alias_id={alias_id})")

        alias.is_deleted = True
        _write_operation_log(
            session, "update", "entity_aliases", alias_id,
            business_action="delete",
            description=f"软删除别名: '{alias.name}' dict_id={alias.dict_id} operator={operator}",
            details={"alias_id": alias_id, "name": alias.name, "dict_id": alias.dict_id,
                      "operator": operator},
            operator=operator,
            trigger_source="soft_delete_entity_alias",
        )
        logger.info(
            f"[DB] soft_delete_entity_alias: alias_id={alias_id} "
            f"'{alias.name}' dict_id={alias.dict_id} operator={operator}"
        )


# ── 审核页动态候选查询 ───────────────────────────────────────────

def find_candidate_dicts_for_review(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None = None,
    top_k: int = 10,
) -> list[dict]:
    """
    审核页实时查询 entity_dict 中与给定实体相似的候选项。
    不过滤 is_verified（未审核的 entity_dict 也要展示给审核员）。

    查询路径：
      1. entity_aliases 精确匹配
      2. entity_aliases name_embedding 向量召回
      3. BGE-Reranker 精排

    返回：[{dict_id, entity_type, name, aliases, canonical_source,
            is_verified, match_type, score}, ...]
    """
    from db.session import get_session
    from core_engine.cloud_api_client import generate_embedding

    entity_data = entity_data or {}

    with get_session() as session:
        if session is None:
            return []

        raw_results: dict[int, dict] = {}  # dict_id → {dict_id, name, match_type}

        # 1. 别名精确匹配
        exact_rows = (
            session.query(EntityAlias.dict_id, EntityAlias.name)
            .filter(
                EntityAlias.name == entity_name.strip(),
                EntityAlias.is_deleted == False,
            )
            .all()
        )
        for row in exact_rows:
            raw_results[row.dict_id] = {
                "dict_id": row.dict_id,
                "name": row.name,
                "match_type": "exact_alias",
            }

        # 2. 向量召回（top_k * 2 给 reranker 留余量）
        embedding = generate_embedding(entity_name)
        if embedding:
            try:
                dist = EntityAlias.name_embedding.cosine_distance(embedding)
                vector_rows = (
                    session.query(EntityAlias.dict_id, EntityAlias.name)
                    .join(EntityDict, EntityAlias.dict_id == EntityDict.id)
                    .filter(
                        EntityAlias.is_deleted == False,
                        EntityDict.is_deleted == False,
                        EntityDict.entity_type == entity_type,
                        EntityAlias.name_embedding.isnot(None),
                    )
                    .order_by(dist)
                    .limit(top_k * 2)
                    .all()
                )
                for row in vector_rows:
                    if row.dict_id not in raw_results:
                        raw_results[row.dict_id] = {
                            "dict_id": row.dict_id,
                            "name": row.name,
                            "match_type": "vector",
                        }
            except Exception as e:
                logger.warning(f"[DB] find_candidate_dicts_for_review 向量搜索失败: {e}")

        if not raw_results:
            return []

        # 3. 批量查 entity_dict 和 aliases
        all_dict_ids = list(raw_results.keys())
        dict_rows = (
            session.query(EntityDict)
            .filter(EntityDict.id.in_(all_dict_ids))
            .all()
        )
        dict_map = {d.id: d for d in dict_rows}

        alias_rows = (
            session.query(EntityAlias.dict_id, EntityAlias.name)
            .filter(
                EntityAlias.dict_id.in_(all_dict_ids),
                EntityAlias.is_deleted == False,
            )
            .all()
        )
        aliases_by_dict: dict[int, list[str]] = {}
        for did, name in alias_rows:
            aliases_by_dict.setdefault(did, []).append(name)

        # 4. Reranker 精排
        rerank_scores = _rerank_candidates(entity_name, entity_data, raw_results, dict_map, aliases_by_dict)

        # 5. 组装结果，按 reranker 分数降序
        output = []
        for dict_id, r in raw_results.items():
            d = dict_map.get(dict_id)
            if d is None:
                continue
            score = rerank_scores.get(dict_id)
            if score is None:
                continue  # reranker 未返回分数，跳过
            output.append({
                "dict_id": dict_id,
                "entity_type": d.entity_type,
                "name": r["name"],
                "aliases": aliases_by_dict.get(dict_id, []),
                "canonical_source": d.canonical_source or "",
                "is_verified": d.is_verified,
                "match_type": r["match_type"],
                "score": round(score, 4),
            })

        output.sort(key=lambda x: x["score"], reverse=True)
        return output[:top_k]


def _rerank_candidates(
    entity_name: str,
    entity_data: dict,
    raw_results: dict[int, dict],
    dict_map: dict[int, EntityDict],
    aliases_by_dict: dict[int, list[str]],
) -> dict[int, float]:
    """
    调用 BGE-Reranker 对候选 entity_dict 精排，返回 {dict_id: score}。
    """
    import requests
    from config.settings import APP

    if not raw_results:
        return {}

    # 构造 query 文本（多语言名称拼接）
    query_parts = [entity_name]
    if entity_data:
        en = entity_data.get("name", "").strip()
        if en and en != entity_name:
            query_parts.append(en)
        en2 = entity_data.get("name_en", "").strip()
        if en2:
            query_parts.append(en2)
    query = " | ".join(query_parts)

    # 构造每个候选的 text（名称 + aliases）
    dict_ids_ordered = list(raw_results.keys())
    texts = []
    for did in dict_ids_ordered:
        parts = [raw_results[did]["name"]]
        d = dict_map.get(did)
        if d:
            cd = d.canonical_data or {}
            cd_name = cd.get("name", "").strip()
            if cd_name and cd_name != parts[0]:
                parts.append(cd_name)
            cd_name_en = cd.get("name_en", "").strip()
            if cd_name_en:
                parts.append(cd_name_en)
        for alias in aliases_by_dict.get(did, []):
            if alias not in parts:
                parts.append(alias)
        texts.append(" | ".join(parts))

    base = APP.RERANK_API_BASE.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    rerank_url = base + "/rerank"

    try:
        resp = requests.post(
            rerank_url,
            json={"query": query, "texts": texts},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            items = data
        else:
            items = data.get("results", [])
        scores: dict[int, float] = {}
        for item in items:
            idx = item.get("index")
            if idx is not None and 0 <= idx < len(dict_ids_ordered):
                scores[dict_ids_ordered[idx]] = item.get("relevance_score", 0.0)
        return scores
    except Exception as e:
        logger.warning(f"[DB] _rerank_candidates 失败: {e}")
        # 降级：用 match_type 给粗略分
        fallback: dict[int, float] = {}
        for did, r in raw_results.items():
            fallback[did] = 1.0 if r["match_type"] == "exact_alias" else 0.5
        return fallback


def get_entities_for_weknora_graph(limit: int = 500, offset: int = 0) -> list[dict]:
    """
    查询审核通过且 weknora_graph_status='pending' 的实体。
    返回字段：id, entity_type, entity_name, entity_data, source_site,
             confidence_score, review_status, is_deleted
    entity_data 中额外注入：
      - name: 与 entity_name 相同
      - name_aliases: 从 entity_aliases 查到的别名列表
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        query = (
            session.query(ExtractedEntity)
            .filter(
                ExtractedEntity.review_status.in_(["approved", "auto_approved"]),
                ExtractedEntity.weknora_graph_status == "pending",
                ExtractedEntity.is_deleted == False,
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
                    EntityAlias.is_deleted == False,
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
    from db.session import get_session

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
    from db.session import get_session

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
    from db.session import get_session

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
                EntityRelation.is_deleted == False,
                FromEntity.is_deleted == False,
                ToEntity.is_deleted == False,
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
    from db.session import get_session

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
    from db.session import get_session

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
    from db.session import get_session

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
                model.is_deleted == False,
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


# ═══════════════════════════════════════════════════════════════════════
# 10. 日志接口
# ═══════════════════════════════════════════════════════════════════════

def log_crawl_task(
    site_key: str,
    task_type: str,
    config_snapshot: dict = {},
) -> int:
    """
    爬取任务开始时插入 crawl_log 记录。
    返回 log_id，供后续 update_crawl_log 使用。失败返回 0。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return 0
        log = CrawlLog(
            site_key=site_key,
            task_type=task_type,
            status="running",
            crawl_config_snapshot=dict(config_snapshot),
        )
        session.add(log)
        session.flush()
        log_id = log.id
        _write_operation_log(
            session, "insert", "crawl_log", log_id,
            business_action="crawl",
            description=f"爬取任务开始: site={site_key} type={task_type}",
            details={"site_key": site_key, "task_type": task_type, "config_snapshot": dict(config_snapshot)},
            trigger_source="log_crawl_task",
        )
        logger.info(f"[DB] 爬取任务日志创建: site={site_key} log_id={log_id}")
        return log_id


def update_crawl_log(
    log_id: int,
    status: str,
    total: int = 0,
    passed: int = 0,
    failed: int = 0,
    error: str = "",
) -> None:
    """爬取任务结束时更新 crawl_log 记录。status: success / failed / partial"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        log = session.query(CrawlLog).filter(CrawlLog.id == log_id).first()
        if log is None:
            logger.warning(f"[DB] update_crawl_log: log_id {log_id} 不存在")
            return
        log.status = status
        log.total_count = total
        log.passed_count = passed
        log.failed_count = failed
        log.error_message = error
        log.finished_at = datetime.now(timezone.utc)
        _write_operation_log(
            session, "update", "crawl_log", log_id,
            business_action="crawl",
            description=f"爬取任务结束: site={log.site_key} status={status} total={total} passed={passed} failed={failed}",
            details={"site_key": log.site_key, "task_type": log.task_type,
                      "status": status, "total": total, "passed": passed, "failed": failed, "error": error},
            trigger_source="update_crawl_log",
        )


def log_review_action(
    object_type: str,
    object_id: int,
    action: str,
    reviewer: str,
    note: str = "",
    before_data: dict = {},
    after_data: dict = {},
) -> None:
    """写入 review_log 审核操作记录。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        _write_review_log(
            session, object_type, object_id, action, reviewer, note,
            dict(before_data), dict(after_data),
        )
        _write_operation_log(
            session, "insert", "review_log", object_id,
            business_action="review",
            description=f"审核操作: {action} {object_type}={object_id} by {reviewer}",
            details={"object_type": object_type, "action": action,
                      "reviewer": reviewer, "note": note,
                      "before_data": dict(before_data), "after_data": dict(after_data)},
            operator=reviewer,
            trigger_source="log_review_action",
        )


# ═══════════════════════════════════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════════════════════════════════

def _write_review_log(
    session,
    object_type: str,
    object_id: int,
    action: str,
    reviewer: str,
    note: str,
    before_data: dict,
    after_data: dict,
) -> None:
    """在已有 session 中写 review_log，不自行 commit。"""
    session.add(ReviewLog(
        object_type=object_type,
        object_id=object_id,
        action=action,
        reviewer=reviewer,
        note=note,
        before_data=before_data,
        after_data=after_data,
    ))


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """将 ISO 格式字符串解析为 datetime，失败返回 None。"""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def _entity_to_dict(r: ExtractedEntity) -> dict:
    return {
        "id": r.id,
        "source_doc_uuid": r.source_doc_uuid,
        "entity_type": r.entity_type,
        "entity_name": r.entity_name,
        "entity_data": r.entity_data,
        "source_text": r.source_text,
        "dict_id": r.dict_id,
        "confidence_score": r.confidence_score,
        "confidence_reason": r.confidence_reason,
        "review_status": r.review_status,
        "weknora_graph_status": r.weknora_graph_status,
    }


def _relation_to_dict(r: EntityRelation) -> dict:
    return {
        "id": r.id,
        "source_doc_uuid": r.source_doc_uuid,
        "from_entity_id": r.from_entity_id,
        "to_entity_id": r.to_entity_id,
        "relation_type": r.relation_type,
        "relation_props": r.relation_props,
        "source_text": r.source_text,
        "confidence_score": r.confidence_score,
        "confidence_reason": r.confidence_reason,
        "review_status": r.review_status,
        "weknora_graph_status": r.weknora_graph_status,
    }


# ═══════════════════════════════════════════════════════════════════════
# 11. 重爬支持
# ═══════════════════════════════════════════════════════════════════════

def reset_document_for_recrawl(uuid: str) -> None:
    """
    重爬前重置文档状态：
      - 软删除该文档的 pending 实体 + 所有关系
      - 已审核实体保留，重新抽取后会走 merge 逻辑更新
      - 重置 extraction_status → pending
      - 重置 weknora_sync_status → pending
      - 清空 extraction_error / extraction_model / extracted_at

    不删除 documents 主记录本身，upsert_document 会覆盖写回新内容。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            logger.warning(f"[DB] reset_document_for_recrawl: Session 未初始化，跳过 {uuid}")
            return

        # 软删除 pending 实体（approved 保留，留给 merge 逻辑更新）
        deleted_ents = (
            session.query(ExtractedEntity)
            .filter(
                ExtractedEntity.source_doc_uuid == uuid,
                ExtractedEntity.review_status == "pending",
                ExtractedEntity.is_deleted == False,
            )
            .update({"is_deleted": True}, synchronize_session=False)
        )
        # 关系全删（文档级重新生成）
        deleted_rels = (
            session.query(EntityRelation)
            .filter(
                EntityRelation.source_doc_uuid == uuid,
                EntityRelation.is_deleted == False,
            )
            .update({"is_deleted": True}, synchronize_session=False)
        )

        # 重置文档流水线状态
        doc = session.query(Document).filter(Document.uuid == uuid).first()
        if doc:
            doc.extraction_status = "pending"
            doc.weknora_sync_status = "pending"
            doc.extraction_error = ""
            doc.extraction_model = ""
            doc.extracted_at = None

        from db.review_lifecycle import recalc_document_state
        recalc_document_state(session, uuid)

        _write_operation_log(
            session, "update", "documents", doc.id if doc else None,
            business_action="recrawl",
            description=f"重爬重置: uuid={uuid} 软删实体={deleted_ents} 软删关系={deleted_rels}",
            details={"uuid": uuid, "deleted_entities": deleted_ents, "deleted_relations": deleted_rels},
            trigger_source="reset_document_for_recrawl",
        )
        logger.info(
            f"[DB] reset_document_for_recrawl: {uuid} "
            f"软删实体={deleted_ents} 软删关系={deleted_rels}"
        )
