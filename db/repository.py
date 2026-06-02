"""
数据库仓库层 —— 所有数据操作的统一入口。

设计原则：
  - 所有模块通过此文件操作数据库，不允许直接写 SQL
  - 软删除：所有查询默认过滤 is_deleted=False
  - 幂等性：upsert 基于 uuid ON CONFLICT
  - Session 管理：使用 db/session.py 提供的 get_session()
"""
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import text

from db.models import (
    Document, DocExtra, Attachment,
    EntityDict, EntityAlias, ExtractedEntity, EntityRelation,
    CrawlLog, ReviewLog,
    EntityMergeQueue,
)

# ── 来源优先级（canonical_data 合并用）──────────────────────────────────
_SOURCE_PRIORITY: dict[str, int] = {
    "nmpa": 10, "fda": 10, "fda_press": 10, "fda_drug_safety": 10,
    "fda_drug_recalls": 10, "fda_api_drug_label": 10, "fda_api_enforcement": 10,
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

        session.flush()  # 确保 doc.id 可用

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
        session.add(DocExtra(uuid=uuid, source_site=source_site, extra=extra))
    else:
        row.extra = extra
        row.source_site = source_site


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
    """更新文档的 LLM 抽取状态。status: pending / processing / done / failed"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        doc = session.query(Document).filter(Document.uuid == uuid).first()
        if doc is None:
            logger.warning(f"[DB] update_extraction_status: 文档不存在 {uuid}")
            return
        doc.extraction_status = status
        if model:
            doc.extraction_model = model
        if error:
            doc.extraction_error = error
        if status == "done":
            doc.extracted_at = extracted_at or datetime.now(timezone.utc)


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
        doc.weknora_sync_status = status


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


def get_pending_weknora_sync(limit: int = 100) -> list[dict]:
    """
    查询 weknora_sync_status=pending 且 is_deleted=False 的文档。
    返回字段：uuid, title, source_site, raw_content, publish_date,
             source_url, parent_uuid, doc_type, language, confidence_score,
             parse_method, crawl_timestamp
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
                Document.publish_date,
                Document.source_url,
                Document.parent_uuid,
                Document.doc_type,
                Document.language,
                Document.confidence_score,
                Document.parse_method,
                Document.crawl_timestamp,
            )
            .filter(
                Document.weknora_sync_status == "pending",
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

# 这些字段合并风险低，差异直接跳过不排队
_SKIP_MERGE_FIELDS = frozenset({"name", "name_en", "aliases", "source_text"})


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

    auto_count = 0
    conflict_count = 0

    for key, new_val in entity_data.items():
        if key in _SKIP_MERGE_FIELDS or new_val is None:
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

        # 4) 值相同 → 跳过
        if existing_val == new_val:
            continue

        # 5) 新来源优先级 >= 当前来源 → auto merge
        if new_priority >= current_priority:
            current[key] = new_val
            auto_count += 1
            continue

        # 6) 同优先级不同源 → 人工审核
        if new_priority == current_priority:
            existing_record = session.query(EntityMergeQueue).filter(
                EntityMergeQueue.source_entity_id == source_entity_id,
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
                    resolution="pending",
                ))
                conflict_count += 1
            continue

        # 7) 低优来源 → 跳过，不覆盖
        pass

    if auto_count > 0:
        entity.canonical_data = current
        entity.canonical_updated_at = datetime.now(timezone.utc)

    if auto_count or conflict_count:
        logger.info(
            f"[DB] process_entity_merge: dict_id={dict_id} "
            f"auto_merged={auto_count} conflicts={conflict_count}"
        )

    return auto_count, conflict_count


def get_pending_merges(limit: int = 50) -> list[dict]:
    """查询 resolution=pending 的合并队列条目。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(EntityMergeQueue)
            .filter(EntityMergeQueue.resolution == "pending")
            .order_by(EntityMergeQueue.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "source_entity_id": r.source_entity_id,
                "target_dict_id": r.target_dict_id,
                "field_name": r.field_name,
                "existing_value": r.existing_value,
                "new_value": r.new_value,
                "source_site": r.source_site,
                "resolution": r.resolution,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


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

        logger.info(f"[DB] resolve_merge: merge_id={merge_id} resolution={resolution}")


# ═══════════════════════════════════════════════════════════════════════
# 4. EntityAlias Repository（消歧核心）
# ═══════════════════════════════════════════════════════════════════════

def lookup_alias(name: str, entity_type: str) -> Optional[int]:
    """
    精确匹配别名表。
    在 entity_aliases 中查找 name 完全匹配且对应 entity_dict.entity_type 匹配的记录。
    命中返回 dict_id，未命中返回 None。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return None
        # JOIN entity_dict 验证 entity_type
        result = (
            session.query(EntityAlias.dict_id)
            .join(EntityDict, EntityAlias.dict_id == EntityDict.id)
            .filter(
                EntityAlias.name == name,
                EntityAlias.is_deleted == False,
                EntityDict.entity_type == entity_type,
                EntityDict.is_deleted == False,
            )
            .first()
        )
        if result:
            return result.dict_id
        return None


def search_similar_entities(
    embedding: list[float],
    entity_type: str,
    top_k: int = 10,
) -> list[dict]:
    """
    向量相似度召回（pgvector，仅做候选召回，分数供参考不直接决策）。
    在 entity_aliases.name_embedding 上做余弦近邻搜索。
    返回 top_k 条，每条包含：dict_id, name, similarity_score, canonical_data, aliases
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        try:
            vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
            sql = text("""
                SELECT
                    ea.dict_id,
                    ea.name,
                    1 - (ea.name_embedding <=> CAST(:vec AS vector)) AS similarity_score,
                    ed.canonical_data
                FROM entity_aliases ea
                JOIN entity_dict ed ON ea.dict_id = ed.id
                WHERE ea.is_deleted = FALSE
                  AND ed.is_deleted = FALSE
                  AND ed.entity_type = :entity_type
                  AND ea.name_embedding IS NOT NULL
                ORDER BY ea.name_embedding <=> CAST(:vec AS vector)
                LIMIT :top_k
            """)
            rows = session.execute(
                sql,
                {"vec": vec_str, "entity_type": entity_type, "top_k": top_k},
            ).fetchall()
            # 一次性查所有候选的 aliases，避免 N+1
            all_dict_ids = [r.dict_id for r in rows]
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


def add_aliases(dict_id: int, aliases: list[tuple[str, str, bool]]) -> None:
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
                .filter(EntityAlias.dict_id == dict_id, EntityAlias.name == name)
                .first()
            )
            if exists is None:
                session.add(EntityAlias(
                    dict_id=dict_id,
                    name=name,
                    lang=lang or "",
                    is_canonical=bool(is_canonical),
                ))


def lookup_external_id(
    entity_type: str,
    external_key: str,
    external_value: str,
) -> dict | None:
    """
    在 entity_dict.external_ids JSONB 中查找匹配的外部 ID。
    external_ids 的结构如 {"stock_code": "000001", "cas_number": "50-78-2"}。
    命中返回 {"dict_id": int, "canonical_name": str}，未命中返回 None。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return None
        row = session.query(
            EntityDict.id,
            EntityDict.canonical_data,
        ).filter(
            EntityDict.entity_type == entity_type,
            EntityDict.is_deleted == False,
            text(f"entity_dict.external_ids->>'{external_key}' = :val"),
        ).params(val=str(external_value).strip()).first()

        if row:
            canonical = row.canonical_data or {}
            return {
                "dict_id": row.id,
                "canonical_name": canonical.get("name", ""),
            }
        return None


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
    lang: str = "",
    is_canonical: bool = False,
    embedding: list[float] | None = None,
) -> None:
    """
    添加别名并附带 embedding 向量。
    别名已存在则跳过；embedding 通过 raw SQL 写入（ORM 未映射该列）。
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        exists = session.query(EntityAlias).filter(
            EntityAlias.dict_id == dict_id, EntityAlias.name == name
        ).first()
        if exists is not None:
            return

        alias = EntityAlias(
            dict_id=dict_id,
            name=name,
            lang=lang or "",
            is_canonical=bool(is_canonical),
        )
        session.add(alias)
        session.flush()

        if embedding:
            vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
            session.execute(
                text("UPDATE entity_aliases SET name_embedding = CAST(:vec AS vector) WHERE id = :id"),
                {"vec": vec_str, "id": alias.id},
            )


# ═══════════════════════════════════════════════════════════════════════
# 5. 抽取结果写入（核心：实体 + 关系 + 状态更新，同一事务）
# ═══════════════════════════════════════════════════════════════════════

def insert_extraction_result(
    source_doc_uuid: str,
    source_site: str,
    entities: list[dict],
    relations: list[dict],
) -> list[int]:
    """
    批量写入 LLM 抽取结果（实体 + 关系），同一事务。

    参数：
        entities:  [ExtractedEntityRecord.model_dump(), ...]
                   必须字段：entity_type, entity_name
                   可选字段：entity_data, source_text, dict_id,
                             confidence_score, confidence_reason
        relations: [ExtractedRelationRecord.model_dump(), ...]
                   必须字段：from_entity_idx, to_entity_idx, relation_type
                   可选字段：relation_props, source_text,
                             confidence_score, confidence_reason

    返回：
        插入的 extracted_entities.id 列表（按输入顺序），失败返回 []
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []

        # ── 写实体（批量 add，单次 flush）─────────────────────────────
        entity_ids: list[int] = []
        ent_objs = []
        for rec in entities:
            ent = ExtractedEntity(
                source_doc_uuid=source_doc_uuid,
                source_site=source_site,
                entity_type=rec["entity_type"],
                entity_name=rec.get("entity_name", ""),
                entity_data=rec.get("entity_data", {}),
                source_text=rec.get("source_text", ""),
                dict_id=rec.get("dict_id"),
                confidence_score=rec.get("confidence_score", 0.0),
                confidence_reason=rec.get("confidence_reason", ""),
                review_status="pending",
            )
            ent_objs.append(ent)
        session.add_all(ent_objs)
        session.flush()  # 单次 flush 获取全部自增 id
        entity_ids = [ent.id for ent in ent_objs]

        # ── 写关系（批量 add，from/to 下标 → 刚插入的 id）───────────
        rel_objs = []
        for rel in relations:
            from_idx = rel.get("from_entity_idx")
            to_idx = rel.get("to_entity_idx")

            if from_idx is None or to_idx is None:
                continue
            if from_idx >= len(entity_ids) or to_idx >= len(entity_ids):
                logger.warning(
                    f"[DB] insert_extraction_result: 关系下标越界 "
                    f"from={from_idx} to={to_idx} entities_len={len(entity_ids)}"
                )
                continue

            rel_objs.append(EntityRelation(
                source_doc_uuid=source_doc_uuid,
                source_site=source_site,
                from_entity_id=entity_ids[from_idx],
                to_entity_id=entity_ids[to_idx],
                relation_type=rel["relation_type"],
                relation_props=rel.get("relation_props", {}),
                source_text=rel.get("source_text", ""),
                confidence_score=rel.get("confidence_score", 0.0),
                confidence_reason=rel.get("confidence_reason", ""),
                review_status="pending",
            ))
        if rel_objs:
            session.add_all(rel_objs)

        # ── 更新文档抽取状态 ──────────────────────────────────────────
        doc = session.query(Document).filter(Document.uuid == source_doc_uuid).first()
        if doc:
            doc.extraction_status = "done"
            doc.extracted_at = datetime.now(timezone.utc)

        logger.info(
            f"[DB] 抽取结果入库: doc={source_doc_uuid} "
            f"entities={len(entity_ids)} relations={len(relations)}"
        )
        return entity_ids


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
) -> int | None:
    """
    从抽取实体创建 entity_dict 记录，返回 dict_id。
    已存在同 entity_type + 同 canonical_name 则跳过（返回已有 id）。
    """
    existing = (
        session.query(EntityDict.id)
        .join(EntityAlias, EntityAlias.dict_id == EntityDict.id)
        .filter(
            EntityAlias.name == entity_name,
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
    canonical_data = dict(entity_data)
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
        is_verified=True,
    )
    session.add(entity)
    session.flush()
    logger.info(f"[DB] entity_dict 创建成功: dict_id={entity.id}")
    return entity.id


def _write_aliases_with_embedding(
    session,
    dict_id: int,
    entity_type: str,
    entity_name: str,
    entity_data: dict,
) -> int:
    """将 entity_name + entity_data 中的名称写为 entity_aliases，附带 embedding。返回新增条数。"""
    from core_engine.cloud_api_client import generate_embedding, build_embedding_text

    names_to_add: set[str] = set()
    names_to_add.add(entity_name.strip())

    data_name = entity_data.get("name", "").strip()
    if data_name:
        names_to_add.add(data_name)
    data_name_en = entity_data.get("name_en", "").strip()
    if data_name_en:
        names_to_add.add(data_name_en)
    for alias in entity_data.get("aliases", []):
        alias_str = str(alias).strip()
        if alias_str:
            names_to_add.add(alias_str)

    logger.info(f"[DB] alias write-back: dict_id={dict_id} names={list(names_to_add)}")

    added = 0
    for name in names_to_add:
        if not name:
            continue
        exists = (
            session.query(EntityAlias)
            .filter(EntityAlias.dict_id == dict_id, EntityAlias.name == name)
            .first()
        )
        if exists is not None:
            logger.debug(f"[DB] alias 已存在: '{name}' dict_id={dict_id}，跳过")
            continue

        embed_text = build_embedding_text(name, entity_type, entity_data)
        embedding = generate_embedding(embed_text)

        alias = EntityAlias(
            dict_id=dict_id,
            name=name,
            lang="zh" if any('一' <= c <= '鿟' for c in name) else "en",
            is_canonical=(name == entity_data.get("name", "")),
        )
        session.add(alias)
        session.flush()

        if embedding:
            vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
            session.execute(
                text("UPDATE entity_aliases SET name_embedding = CAST(:vec AS vector) WHERE id = :id"),
                {"vec": vec_str, "id": alias.id},
            )
            logger.debug(f"[DB] alias '{name}' written with embedding dims={len(embedding)}")
        else:
            logger.warning(f"[DB] alias '{name}' embedding 生成失败")
        added += 1
    return added


def approve_entity_with_alias_writeback(
    entity_id: int,
    reviewer: str,
    note: str = "",
    dict_id: int | None = None,
    force_new: bool = False,
) -> None:
    """
    审核通过实体，回写别名 + embedding 到 entity_dict。
    优先级：force_new > dict_id > ent.dict_id > 创建新 entity_dict
    - force_new=True → 忽略一切，以新实体创建
    - dict_id 不为 None → 审核员指定了目标 dict_id
    - 都不给 → 保留消歧结果（ent.dict_id），无则新建
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return

        ent = session.query(ExtractedEntity).filter(ExtractedEntity.id == entity_id).first()
        if ent is None:
            logger.warning(f"[DB] approve_entity_with_alias_writeback: 实体 {entity_id} 不存在")
            return

        before = ent.review_status
        ent.review_status = "approved"
        ent.reviewer_note = note
        ent.reviewed_by = reviewer
        ent.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        _write_review_log(session, "entity", entity_id, "approve", reviewer, note,
                          {"review_status": before}, {"review_status": "approved"})

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
            dict_id = _create_entity_dict_from_extraction(
                session,
                entity_name=ent.entity_name,
                entity_type=ent.entity_type,
                entity_data=entity_data,
                source_site=ent.source_site or "",
            )
            if dict_id:
                ent.dict_id = dict_id
                session.flush()
            else:
                logger.warning(f"[DB] 无法创建 entity_dict，跳过 alias 回写: entity_id={entity_id}")
                return

        # 回写别名
        added = _write_aliases_with_embedding(
            session, dict_id, ent.entity_type, ent.entity_name, entity_data,
        )
        if added:
            logger.info(f"[DB] alias write-back: entity_id={entity_id} dict_id={dict_id} added {added} aliases")

        # 合并 entity_data 到 canonical_data
        merge_auto, merge_conflict = process_entity_merge(
            session, dict_id, entity_id, entity_data, ent.source_site or "",
        )
        if merge_auto or merge_conflict:
            logger.info(
                f"[DB] merge: entity_id={entity_id} dict_id={dict_id} "
                f"auto_merged={merge_auto} conflicts={merge_conflict}"
            )


def reject_entity(id: int, reviewer: str, note: str = "") -> None:
    """审核拒绝实体，同时写 review_log。"""
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
        
        # 批量更新（含 writeback）
        alias_count = 0
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
                )
                if dict_id:
                    ent.dict_id = dict_id
                    session.flush()

            if dict_id:
                added = _write_aliases_with_embedding(
                    session, dict_id, ent.entity_type, ent.entity_name, entity_data,
                )
                alias_count += added
                process_entity_merge(
                    session, dict_id, ent.id, entity_data, ent.source_site or "",
                )

        session.commit()
        logger.info(f"[DB] batch_approve_entities: {count} entities approved, {alias_count} aliases written")
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
    """审核通过关系，同时写 review_log。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        rel = session.query(EntityRelation).filter(EntityRelation.id == id).first()
        if rel is None:
            logger.warning(f"[DB] approve_relation: 关系 {id} 不存在")
            return
        before = rel.review_status
        rel.review_status = "approved"
        rel.reviewer_note = note
        rel.reviewed_by = reviewer
        rel.reviewed_at = datetime.now(timezone.utc)
        session.flush()
        _write_review_log(session, "relation", id, "approve", reviewer, note,
                          {"review_status": before}, {"review_status": "approved"})


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
        return {"success": True, "message": f"成功批量拒绝 {count} 个关系", "count": count}


# ═══════════════════════════════════════════════════════════════════════
# 8. WeKnora 图同步状态接口
# ═══════════════════════════════════════════════════════════════════════

def get_entity_dict_for_weknora(limit: int = 500) -> list[dict]:
    """
    查询 weknora_sync_status='pending' 且 is_deleted=False 的 entity_dict 行。
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
            )
        )
        if limit:
            q = q.limit(limit)
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
        if error:
            logger.warning(f"[DB] 批量标记 entity_dict failed: {len(ids)} 条, error={error[:200]}")
        else:
            logger.warning(f"[DB] 批量标记 entity_dict failed: {len(ids)} 条")


def get_entities_for_weknora_graph(limit: int = 500) -> list[dict]:
    """
    查询审核通过且 weknora_graph_status='pending' 的实体。
    返回字段：id, entity_type, entity_name, entity_data, source_site,
             confidence_score, review_status, is_deleted
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
        rows = query.all()
        return [
            {
                "id": r.id,
                "dict_id": r.dict_id,
                "entity_type": r.entity_type,
                "entity_name": r.entity_name,
                "entity_data": r.entity_data or {},
                "source_site": r.source_site or "",
                "confidence_score": r.confidence_score or 0.0,
                "review_status": r.review_status,
                "is_deleted": r.is_deleted,
            }
            for r in rows
        ]


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
        if error:
            logger.warning(f"[DB] 批量标记实体 graph failed: {len(entity_ids)} 条, error={error[:200]}")
        else:
            logger.warning(f"[DB] 批量标记实体 graph failed: {len(entity_ids)} 条")


def get_relations_for_weknora_graph(limit: int = 500) -> list[dict]:
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
            )
        )
        if limit > 0:
            query = query.limit(limit)
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
      - 软删除该文档的所有 extracted_entities 和 entity_relations
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

        # 软删除旧抽取结果
        deleted_ents = (
            session.query(ExtractedEntity)
            .filter(
                ExtractedEntity.source_doc_uuid == uuid,
                ExtractedEntity.is_deleted == False,
            )
            .update({"is_deleted": True}, synchronize_session=False)
        )
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

        logger.info(
            f"[DB] reset_document_for_recrawl: {uuid} "
            f"软删实体={deleted_ents} 软删关系={deleted_rels}"
        )
