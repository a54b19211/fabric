"""
数据库仓库层 —— 所有数据操作的统一入口。

设计原则：
  - 所有模块通过此文件操作数据库，不允许直接写 SQL
  - 软删除：所有查询默认过滤 is_deleted=False
  - 幂等性：upsert 基于 uuid ON CONFLICT
  - Session 管理：使用 db/session.py 提供的 get_session()
"""
import re
import json
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

def upsert_document(envelope) -> dict | None:
    """
    幂等写入文档。同一 uuid 重复调用只更新，不新增。
    同时写入：documents + doc_extra（extra 非空时）+ attachments（有附件时）。
    参数 envelope 是 MetadataEnvelope 对象。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            raise RuntimeError("数据库未配置，文档未保存")

        # ── 1. documents 表 ──────────────────────────────────────────
        doc = session.query(Document).filter(Document.uuid == envelope.uuid).with_for_update().first()
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
                lineage=json.dumps(envelope.lineage or {}, ensure_ascii=False),
                weknora_sync_status="pending",
                extraction_status="pending",
            )
            session.add(doc)
            logger.info(f"[DB] 新增文档: {envelope.uuid}")
            is_new = True
        else:
            # 旧链路没有版本化发布：变化先拒写，不能沿用旧抽取/审核成果盖成新正文。
            incoming = envelope.raw_content or ""
            if doc.raw_content and incoming and doc.raw_content != incoming:
                from db.publication import enqueue_changed_document

                return enqueue_changed_document(session, envelope)
            # 只更新可变字段，保留 extraction_status / weknora_sync_status 等流水线状态
            doc.title = envelope.title or doc.title
            doc.summary = envelope.summary or doc.summary
            doc.raw_content = envelope.raw_content or doc.raw_content
            doc.source_url = envelope.provenance.source_url or doc.source_url
            doc.parse_method = envelope.parse_method or doc.parse_method
            doc.doc_type = envelope.doc_type or doc.doc_type
            doc.language = envelope.language or doc.language
            if envelope.lineage:
                doc.lineage = json.dumps(envelope.lineage, ensure_ascii=False)
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

        from db.publication import enqueue_current_document

        enqueue_current_document(session, envelope.uuid)
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


def create_identity_merge_proposal(source_entity_id, target_dict_id, agent_reason='', agent_confidence=0.0,
                                   agent_run_id='', session=None, **metadata):
    from db.identity import use_session, add_identity_proposal
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
    from db.identity import resolve_link_proposal
    return resolve_link_proposal(proposal_id, status, reviewer, note, session=session, review_context=review_context)


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
                from db.identity import set_dictionary_identity_data
                set_dictionary_identity_data(session, entity, canonical_data=cd)
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
    session=None, review_context=None,
) -> None:
    """
    解决合并冲突。
    resolution: accept_new | keep_existing | custom
    custom 时需要传 resolved_value。
    """
    from api.review_auth import require_trusted_review_context
    from db.identity import use_session
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
                from db.identity import set_dictionary_identity_data
                set_dictionary_identity_data(session, entity, canonical_data=canonical)
                entity.canonical_updated_at = datetime.now(timezone.utc)
        elif resolution == "custom" and resolved_value is not None:
            merge.resolved_value = resolved_value
            entity = session.query(EntityDict).filter(EntityDict.id == merge.target_dict_id).first()
            if entity:
                canonical = dict(entity.canonical_data or {})
                canonical[merge.field_name] = resolved_value
                from db.identity import set_dictionary_identity_data
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
    from db.identity import use_session
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
                    EntityDict.is_deleted == False,
                ).first()
                if entity:
                    canonical = dict(entity.canonical_data or {})
                    new_val = merge.new_value.get("_val") if isinstance(merge.new_value, dict) and list(merge.new_value.keys()) == ["_val"] else merge.new_value
                    canonical[merge.field_name] = new_val
                    from db.identity import set_dictionary_identity_data
                    set_dictionary_identity_data(session, entity, canonical_data=canonical)
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
                    from db.identity import set_dictionary_identity_data
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


def merge_entities(keeper_dict_id, absorbed_dict_ids, field_decisions, reviewer='', note='', session=None, review_context=None):
    from db.identity import use_session, merge_dictionary_identities
    with use_session(session) as active:
        return merge_dictionary_identities(active, keeper_dict_id, absorbed_dict_ids, field_decisions, reviewer, note, review_context=review_context)


def rollback_merge(merge_log_id, reason='', session=None, review_context=None):
    from db.identity import use_session, rollback_identity_merge
    from api.review_auth import require_trusted_review_context
    require_trusted_review_context(review_context)
    with use_session(session) as active:
        return rollback_identity_merge(active, merge_log_id, reason, review_context=review_context)


def get_merge_logs(limit: int = 50, include_rolled_back: bool = True, dict_ids=None, session=None) -> list[dict]:
    """查询合并日志列表。"""
    from db.identity import use_session
    from sqlalchemy import or_

    with use_session(session) as session:

        q = session.query(EntityMergeLog).order_by(EntityMergeLog.created_at.desc())
        if dict_ids is not None:
            selected_ids = sorted({int(value) for value in dict_ids})
            if not selected_ids:
                return []
            q = q.filter(or_(EntityMergeLog.keeper_dict_id.in_(selected_ids),
                             EntityMergeLog.absorbed_dict_ids.overlap(selected_ids)))
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
                "rolled_back_by": r.rolled_back_by,
                "rolled_back_at": r.rolled_back_at.isoformat() if r.rolled_back_at else None,
                "rollback_reason": r.rollback_reason,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


# ═══════════════════════════════════════════════════════════════════════
# 4. EntityAlias Repository（消歧核心）
# ═══════════════════════════════════════════════════════════════════════

def lookup_alias_candidates(name: str, entity_type: str, session=None) -> list[dict]:
    """Return every live same-name dictionary; a homonym is never a unique match."""
    from db.identity import use_session
    with use_session(session) as active:
        rows = active.query(EntityDict.id, EntityDict.canonical_data, EntityDict.external_ids).join(
            EntityAlias, EntityAlias.dict_id == EntityDict.id).filter(
                func.lower(EntityAlias.name) == name.strip().lower(),
                EntityDict.entity_type == entity_type, EntityDict.is_deleted == False,
                EntityAlias.is_deleted == False).distinct().order_by(EntityDict.id).all()
        return [{'dict_id': row.id, 'name': name, 'canonical_name': (row.canonical_data or {}).get('name', ''),
                 'canonical_data': row.canonical_data or {}, 'external_ids': row.external_ids or {}} for row in rows]


def lookup_alias(name: str, entity_type: str, session=None) -> Optional[int]:
    """Compatibility API. Return an ID only when all live same-name matches agree."""
    rows = lookup_alias_candidates(name, entity_type, session=session)
    identities = {row['dict_id'] for row in rows}
    return next(iter(identities)) if len(identities) == 1 else None


def search_similar_entities(
    embedding: list[float],
    entity_type: str,
    top_k: int = 10,
    session=None,
    strict_errors: bool = True,
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
        except Exception:
            logger.error('[DB] Entity similarity lookup unavailable')
            if strict_errors:
                raise RuntimeError('Entity similarity lookup unavailable') from None
            return []

    if session is not None:
        return _query(session)

    from db.session import get_session
    with get_session() as sess:
        if sess is None:
            if strict_errors:
                raise RuntimeError("Entity similarity lookup is not configured")
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


# ── 字典更新级联重算（状态陈旧修复）──────────────────────────────────

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


def _do_insert_extraction_result(session, source_doc_uuid, source_site, entities, relations) -> list[int]:
    """Persist observations and link proposals without turning repeated evidence into approval."""
    from db.identity import (digest, evidence_values, record_evidence, row_evidence_columns,
                             batch_identity_keys, compatible_identity, disambiguation,
                             has_candidates, persist_candidates, native_identity_claim, verified_native_identity)
    from db.models import EntitySourceEvidence
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
    final_ids = []
    batch = []
    for incoming in entities:
        rec = {**incoming, 'entity_data': dict(incoming.get('entity_data') or {})}
        info = dict(disambiguation(rec))
        native_identifier = verified_native_identity(rec, source_site, native_projection)
        if native_identity_claim(rec) and native_identifier is None:
            candidates = list(info.get('candidates') or [])
            proposed_id = rec.get('dict_id') or info.get('dict_id')
            if proposed_id and not any(item.get('dict_id') == proposed_id for item in candidates):
                candidates.append({'dict_id': proposed_id, 'score': info.get('confidence', 0.0)})
            info.update(status='ambiguous', auto_link=False, dict_id=None, candidates=candidates,
                        match_step='step1_native_source_unverified',
                        reason='Native identity does not match the stored source projection')
            rec['dict_id'] = None
        if native_identifier:
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
        if info:
            rec['_disambig'] = info
            rec['entity_data']['_disambig'] = info
        # Intermediate and ambiguous candidates cannot allocate a new dictionary.
        candidate = has_candidates(rec)
        dict_id = None if candidate else (rec.get('dict_id') or info.get('dict_id'))
        if dict_id:
            dictionary = session.query(EntityDict).filter(EntityDict.id == dict_id).populate_existing().with_for_update().first()
            if dictionary is None or dictionary.is_deleted or dictionary.entity_type != rec['entity_type']:
                raise ValueError('Invalid extraction dictionary link')
            if native_identifier:
                namespace, native_field, native_value = native_identifier
                if not any((dictionary.external_ids or {}).get(key) == native_value for key in (namespace, native_field)):
                    raise ValueError('Native dictionary identity changed during persistence')
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
        keys = batch_identity_keys(rec)
        if entity is None and not candidate and not native_identifier:
            for previous_keys, previous_data, previous in batch:
                if (keys & previous_keys and compatible_identity(rec['entity_data'], previous_data) and
                        not (dict_id and previous.dict_id and dict_id != previous.dict_id)):
                    entity = previous
                    break
        if entity is None:
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
        # No approval, canonical overwrite, alias migration or historical bulk repair here.
        observation_key = record_evidence(session, 'entity', entity, rec, source_doc_uuid, source_site)
        if info.get('candidates') or info.get('dict_id'):
            persist_candidates(session, entity, rec, observation_key)
        batch.append((keys, rec['entity_data'], entity))
        final_ids.append(entity.id)
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
    doc = session.query(Document).filter(Document.uuid == source_doc_uuid).first()
    if doc:
        doc.extraction_status = 'done'; doc.extracted_at = datetime.now(timezone.utc)
    from db.review_lifecycle import recalc_document_state
    recalc_document_state(session, source_doc_uuid)
    return final_ids


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
    allow_embedding_generation: bool = True,
) -> int:
    """将 entity_name + entity_data 中的名称写为 entity_aliases，附带 embedding。返回新增条数。"""
    from core_engine.common.cloud_api_client import generate_embedding

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

    # 审核持锁路径只复用缓存；显式允许的兼容调用才生成缺失向量。
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
            embedding = embeddings_cache.get(name)
            if not embedding and allow_embedding_generation:
                embedding = generate_embedding(name)
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
        from db.identity import invalidate_dictionary_confirmation
        invalidate_dictionary_confirmation(session, entity_dict)
        logger.info(
            f"[DB] _sync_canonical_to_extracted: dict_id={dict_id} "
            f"synced {count}/{len(rows)} rows, {len(cd)} canonical fields"
        )

    return count


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
        _write_aliases_with_embedding(active, target_id, ent.entity_type, ent.entity_name,
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


def approve_relation(id, reviewer, note='', evidence=None, session=None, review_context=None):
    from api.review_auth import require_trusted_review_context
    from db.identity import use_session, auditable_identity, auditable_entity_identity, validate_evidence, get_object_evidence, record_evidence, snapshot, review_object_binding, review_endpoint_binding
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
        relation.review_status = 'approved'; relation.reviewer_note = note
        relation.reviewed_by = reviewer; relation.reviewed_at = datetime.now(timezone.utc)
        record_evidence(active, 'relation', relation, {'relation_type': relation.relation_type,
            'from_entity_id': relation.from_entity_id, 'to_entity_id': relation.to_entity_id,
            'evidence': validated}, relation.source_doc_uuid, relation.source_site)
        _write_review_log(active, 'relation', id, 'approve', reviewer, note,
                          {'review_status': before}, {'review_status': 'approved', 'evidence': validated,
                            'review_audit_schema': 'trusted_human_relation_approval.v1',
                            'reviewed_by': relation.reviewed_by, 'reviewed_at': relation.reviewed_at.isoformat(),
                            'object_binding_hash': review_object_binding(relation),
                            'endpoint_identities': {str(key): review_endpoint_binding(by_id[key]) for key in endpoint_ids}})
        from db.review_lifecycle import recalc_document_state
        recalc_document_state(active, relation.source_doc_uuid)
        return {'success': True, 'relation_id': id}


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


def batch_approve_relations(source_doc_uuid='', source_site='', reviewer='', note='',
                            min_confidence=0.0, review_context=None):
    """Apply the same fixed-evidence and endpoint checks to every selected relation."""
    from api.review_auth import require_trusted_review_context
    from db.identity import use_session
    from biz_rules.history_models import HistoryError
    reviewer = require_trusted_review_context(review_context)
    with use_session() as session:
        query = session.query(EntityRelation).filter(EntityRelation.review_status == 'pending',
            EntityRelation.is_deleted == False, EntityRelation.confidence_score >= min_confidence)
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
        except (ValueError, HistoryError) as exc:
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
    from core_engine.common.cloud_api_client import generate_embedding

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
            json={"query": query, "documents": texts},
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


# Evidence and mapping APIs share the same source transaction helpers.
from db.identity import (get_object_evidence, create_primekg_mapping_proposal,
                         get_primekg_mapping_proposals, resolve_primekg_mapping_proposal)


def lookup_external_id_candidates(entity_type, external_key, external_value, session=None):
    from db.identity import use_session
    with use_session(session) as active:
        rows = active.query(EntityDict).filter(EntityDict.entity_type == entity_type,
            EntityDict.is_deleted == False,
            EntityDict.external_ids[external_key].astext == str(external_value).strip()).order_by(EntityDict.id).all()
        return [{'dict_id': row.id, 'canonical_name': (row.canonical_data or {}).get('name', ''),
                 'entity_type': row.entity_type, 'canonical_data': row.canonical_data or {},
                 'external_ids': row.external_ids or {}} for row in rows]
