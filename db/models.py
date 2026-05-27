"""
SQLAlchemy ORM 模型 —— 与 db/schema.sql 保持一致（v3）
10张表，无外键约束，关联由业务层保证。

设计原则：
  - 每张表都有显式自增 id 作为内部主键
  - uuid 作为业务去重键（unique=True）
  - 所有表都有 is_deleted 软删除字段
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text, Float, BigInteger, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.mutable import MutableDict


class Base(DeclarativeBase):
    pass


# ── 1. documents ──────────────────────────────────────────────

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # 业务去重键
    title: Mapped[str] = mapped_column(Text, default="")
    publish_date: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    raw_content: Mapped[str] = mapped_column(Text, default="")   # 查列表时勿 SELECT *
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_site: Mapped[str] = mapped_column(Text, default="")   # nmpa/cde/chictr/pubmed/hkex/...
    parent_uuid: Mapped[str] = mapped_column(Text, default="")   # 附件/子文档指向父文档
    crawl_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.3)
    confidence_reason: Mapped[str] = mapped_column(Text, default="")
    parse_method: Mapped[str] = mapped_column(Text, default="")  # css_extract/trafilatura/mineru+qwen
    doc_status: Mapped[str] = mapped_column(Text, default="AUTO_PROCESSED")
    # AUTO_PROCESSED / PENDING_REVIEW / REVIEWED
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    doc_type: Mapped[str] = mapped_column(Text, default="")
    # regulatory_notice/drug_recall/drug_label/clinical_trial/literature/industry_news/company_announcement/policy
    language: Mapped[str] = mapped_column(Text, default="")      # zh/en
    weknora_sync_status: Mapped[str] = mapped_column(Text, default="pending")   # pending/synced/failed
    extraction_status: Mapped[str] = mapped_column(Text, default="pending")     # pending/processing/done/failed
    extraction_model: Mapped[str] = mapped_column(Text, default="")
    extraction_error: Mapped[str] = mapped_column(Text, default="")
    extracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 2. doc_extra ──────────────────────────────────────────────

class DocExtra(Base):
    """来源特有字段，替代 chictr_meta / pubmed_meta / hkex_meta。"""
    __tablename__ = "doc_extra"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # 与 documents.uuid 对应
    source_site: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 3. attachments ────────────────────────────────────────────

class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    parent_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, default="")
    file_url: Mapped[str] = mapped_column(Text, default="")
    local_path: Mapped[str] = mapped_column(Text, default="")
    file_uuid: Mapped[str] = mapped_column(Text, default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 4. entity_dict ────────────────────────────────────────────

class EntityDict(Base):
    """实体字典主表，每个真实世界实体一行。"""
    __tablename__ = "entity_dict"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_ids: Mapped[dict] = mapped_column(JSONB, default=dict)
    canonical_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 合并规则（代码层定义）：
    #   数组字段（aliases/types）：追加去重
    #   状态字段（phase_cn/global_status）：来源优先级高的覆盖低的
    #   快照字段（market_cap）：时间新的覆盖旧的
    canonical_source: Mapped[str] = mapped_column(Text, default="")
    canonical_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    neo4j_node_id: Mapped[str] = mapped_column(Text, default="")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 5. entity_aliases ─────────────────────────────────────────

class EntityAlias(Base):
    """实体别名查找表，所有名称打平到这里，消歧只需查一列。"""
    __tablename__ = "entity_aliases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dict_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # -> entity_dict.id
    name: Mapped[str] = mapped_column(Text, nullable=False)           # 名称本身
    lang: Mapped[str] = mapped_column(Text, default="")               # zh/en/other
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=False) # 是否标准名
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ── 6. extracted_entities ─────────────────────────────────────

class ExtractedEntity(Base):
    """LLM从文档中抽取的图节点，审核通过后推Neo4j。"""
    __tablename__ = "extracted_entities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_doc_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    source_site: Mapped[str] = mapped_column(Text, default="")
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_name: Mapped[str] = mapped_column(Text, default="")        # LLM抽出的原始名称
    entity_data: Mapped[dict] = mapped_column(JSONB, default=dict)    # 完整字段
    source_text: Mapped[str] = mapped_column(Text, default="")        # LLM抽取时的原文片段
    dict_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # 消歧命中的字典ID
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_reason: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(Text, default="pending")
    # pending / auto_approved / approved / rejected
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(Text, default="")
    neo4j_status: Mapped[str] = mapped_column(Text, default="pending")
    # pending / pushed / failed
    neo4j_node_id: Mapped[str] = mapped_column(Text, default="")      # Neo4j节点ID
    neo4j_pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    neo4j_error: Mapped[str] = mapped_column(Text, default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 7. entity_relations ───────────────────────────────────────

class EntityRelation(Base):
    """LLM从文档中抽取的图边，审核通过后推Neo4j。"""
    __tablename__ = "entity_relations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_doc_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    source_site: Mapped[str] = mapped_column(Text, default="")
    from_entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    to_entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    from_neo4j_id: Mapped[str] = mapped_column(Text, default="")
    to_neo4j_id: Mapped[str] = mapped_column(Text, default="")
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)
    relation_props: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_text: Mapped[str] = mapped_column(Text, default="")        # LLM抽取时的原文片段
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_reason: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(Text, default="pending")
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(Text, default="")
    neo4j_status: Mapped[str] = mapped_column(Text, default="pending")
    neo4j_pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    neo4j_error: Mapped[str] = mapped_column(Text, default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 8. neo4j_sync_log ─────────────────────────────────────────

class Neo4jSyncLog(Base):
    __tablename__ = "neo4j_sync_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)    # 'entity' / 'relation'
    object_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    neo4j_node_id: Mapped[str] = mapped_column(Text, default="")
    operation: Mapped[str] = mapped_column(Text, default="")          # create/merge/update
    status: Mapped[str] = mapped_column(Text, default="")             # success/failed
    error_message: Mapped[str] = mapped_column(Text, default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pushed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 9. crawl_log ──────────────────────────────────────────────

class CrawlLog(Base):
    """爬取任务日志，记录每次爬取的执行情况。"""
    __tablename__ = "crawl_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_key: Mapped[str] = mapped_column(Text, default="")            # nmpa/cde/pubmed/...
    task_type: Mapped[str] = mapped_column(Text, default="")           # full/incremental
    status: Mapped[str] = mapped_column(Text, default="running")       # running/success/failed
    total_count: Mapped[int] = mapped_column(default=0)
    passed_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    crawl_config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # 配置快照
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 10. review_log ────────────────────────────────────────────

class ReviewLog(Base):
    """审核操作日志，记录每次人工审核的操作详情。"""
    __tablename__ = "review_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(Text, nullable=False, default="")  # document/entity/relation
    object_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False, default="")       # approve/reject/update
    reviewer: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    before_data: Mapped[dict] = mapped_column(JSONB, default=dict)   # 操作前快照
    after_data: Mapped[dict] = mapped_column(JSONB, default=dict)    # 操作后快照
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 11. entity_merge_queue ──────────────────────────────────────

class EntityMergeQueue(Base):
    """实体数据合并队列：auto 规则自动合并，同优先级冲突进人工审核。"""
    __tablename__ = "entity_merge_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_dict_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    existing_value: Mapped[dict] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict] = mapped_column(JSONB, nullable=True)
    source_site: Mapped[str] = mapped_column(Text, default="")
    resolution: Mapped[str] = mapped_column(Text, default="pending")
    resolved_value: Mapped[dict] = mapped_column(JSONB, nullable=True)
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
