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
    Neo4jSyncLog, CrawlLog, ReviewLog,
)

# ── 来源优先级（canonical_data 合并用）──────────────────────────────────
_SOURCE_PRIORITY: dict[str, int] = {
    "nmpa": 10, "fda": 10, "fda_press": 10, "fda_drug_safety": 10,
    "fda_drug_recalls": 10, "fda_api_drug_label": 10, "fda_api_enforcement": 10,
    "drugbank": 10,
    "chictr": 8, "clinicaltrials": 8,
    "hkex": 6,
    "cde": 5,
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
    返回字段：uuid, title, source_site, raw_content, publish_date
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
                "raw_content": r.raw_content,
                "publish_date": r.publish_date,
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
    top_k: int = 5,
) -> list[dict]:
    """
    向量相似度检索（pgvector）。
    在 entity_aliases.name_embedding 上做余弦近邻搜索。
    返回 top_k 条，每条包含：dict_id, name, similarity_score
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
                    1 - (ea.name_embedding <=> CAST(:vec AS vector)) AS similarity_score
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
            return [
                {
                    "dict_id": r.dict_id,
                    "name": r.name,
                    "similarity_score": float(r.similarity_score),
                }
                for r in rows
            ]
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

        # ── 写实体 ────────────────────────────────────────────────────
        entity_ids: list[int] = []
        for rec in entities:
            dict_id = rec.get("dict_id")
            confidence = rec.get("confidence_score", 0.0)

            # 消歧命中 + 高置信度 → 自动通过
            if dict_id is not None and confidence >= 0.8:
                review_status = "auto_approved"
            else:
                review_status = "pending"

            ent = ExtractedEntity(
                source_doc_uuid=source_doc_uuid,
                source_site=source_site,
                entity_type=rec["entity_type"],
                entity_name=rec.get("entity_name", ""),
                entity_data=rec.get("entity_data", {}),
                source_text=rec.get("source_text", ""),
                dict_id=dict_id,
                confidence_score=confidence,
                confidence_reason=rec.get("confidence_reason", ""),
                review_status=review_status,
                neo4j_status="pending",
            )
            session.add(ent)
            session.flush()  # 获取自增 id
            entity_ids.append(ent.id)

        # ── 写关系（from/to 下标 → 刚插入的 id）─────────────────────
        for rel in relations:
            from_idx = rel.get("from_entity_idx")
            to_idx = rel.get("to_entity_idx")

            # 下标越界则跳过，不阻断整批
            if from_idx is None or to_idx is None:
                continue
            if from_idx >= len(entity_ids) or to_idx >= len(entity_ids):
                logger.warning(
                    f"[DB] insert_extraction_result: 关系下标越界 "
                    f"from={from_idx} to={to_idx} entities_len={len(entity_ids)}"
                )
                continue

            confidence = rel.get("confidence_score", 0.0)
            review_status = "auto_approved" if confidence >= 0.8 else "pending"

            session.add(EntityRelation(
                source_doc_uuid=source_doc_uuid,
                source_site=source_site,
                from_entity_id=entity_ids[from_idx],
                to_entity_id=entity_ids[to_idx],
                relation_type=rel["relation_type"],
                relation_props=rel.get("relation_props", {}),
                source_text=rel.get("source_text", ""),
                confidence_score=confidence,
                confidence_reason=rel.get("confidence_reason", ""),
                review_status=review_status,
                neo4j_status="pending",
            ))

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


# ═══════════════════════════════════════════════════════════════════════
# 8. Neo4j 推送状态接口
# ═══════════════════════════════════════════════════════════════════════

def get_approved_for_neo4j(limit: int = 100) -> list[dict]:
    """
    查询 review_status=approved 且 neo4j_status=pending 且 is_deleted=False 的实体。
    返回字段：id, entity_type, entity_name, entity_data, dict_id
    """
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(ExtractedEntity)
            .filter(
                ExtractedEntity.review_status.in_(["approved", "auto_approved"]),
                ExtractedEntity.neo4j_status == "pending",
                ExtractedEntity.is_deleted == False,
            )
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "entity_type": r.entity_type,
                "entity_name": r.entity_name,
                "entity_data": r.entity_data,
                "dict_id": r.dict_id,
            }
            for r in rows
        ]


def get_approved_relations_for_neo4j(limit: int = 100) -> list[dict]:
    """查询审核通过且待推送的关系。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return []
        rows = (
            session.query(EntityRelation)
            .filter(
                EntityRelation.review_status.in_(["approved", "auto_approved"]),
                EntityRelation.neo4j_status == "pending",
                EntityRelation.is_deleted == False,
            )
            .limit(limit)
            .all()
        )
        return [_relation_to_dict(r) for r in rows]


def mark_entity_pushed(id: int, neo4j_node_id: str) -> None:
    """标记实体推送成功，回填 neo4j_node_id，更新 neo4j_status=pushed。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        ent = session.query(ExtractedEntity).filter(ExtractedEntity.id == id).first()
        if ent is None:
            return
        ent.neo4j_status = "pushed"
        ent.neo4j_node_id = neo4j_node_id
        ent.neo4j_pushed_at = datetime.now(timezone.utc)
        ent.neo4j_error = ""
        session.flush()
        session.add(Neo4jSyncLog(
            object_type="entity",
            object_id=id,
            neo4j_node_id=neo4j_node_id,
            operation="create",
            status="success",
        ))


def mark_entity_failed(id: int, error: str) -> None:
    """标记实体推送失败，记录错误原因，更新 neo4j_status=failed。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        ent = session.query(ExtractedEntity).filter(ExtractedEntity.id == id).first()
        if ent is None:
            return
        ent.neo4j_status = "failed"
        ent.neo4j_error = error
        session.flush()
        session.add(Neo4jSyncLog(
            object_type="entity",
            object_id=id,
            operation="create",
            status="failed",
            error_message=error,
        ))


def mark_relation_pushed(id: int, from_neo4j_id: str, to_neo4j_id: str) -> None:
    """标记关系推送成功。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        rel = session.query(EntityRelation).filter(EntityRelation.id == id).first()
        if rel is None:
            return
        rel.neo4j_status = "pushed"
        rel.from_neo4j_id = from_neo4j_id
        rel.to_neo4j_id = to_neo4j_id
        rel.neo4j_pushed_at = datetime.now(timezone.utc)
        rel.neo4j_error = ""
        session.flush()
        session.add(Neo4jSyncLog(
            object_type="relation",
            object_id=id,
            neo4j_node_id=f"{from_neo4j_id}->{to_neo4j_id}",
            operation="create",
            status="success",
        ))


def mark_relation_failed(id: int, error: str) -> None:
    """标记关系推送失败。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        rel = session.query(EntityRelation).filter(EntityRelation.id == id).first()
        if rel is None:
            return
        rel.neo4j_status = "failed"
        rel.neo4j_error = error
        session.flush()
        session.add(Neo4jSyncLog(
            object_type="relation",
            object_id=id,
            operation="create",
            status="failed",
            error_message=error,
        ))


# ═══════════════════════════════════════════════════════════════════════
# 9. 日志接口
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
        "neo4j_status": r.neo4j_status,
    }


def _relation_to_dict(r: EntityRelation) -> dict:
    return {
        "id": r.id,
        "source_doc_uuid": r.source_doc_uuid,
        "from_entity_id": r.from_entity_id,
        "to_entity_id": r.to_entity_id,
        "from_neo4j_id": r.from_neo4j_id,
        "to_neo4j_id": r.to_neo4j_id,
        "relation_type": r.relation_type,
        "relation_props": r.relation_props,
        "source_text": r.source_text,
        "confidence_score": r.confidence_score,
        "confidence_reason": r.confidence_reason,
        "review_status": r.review_status,
        "neo4j_status": r.neo4j_status,
    }
