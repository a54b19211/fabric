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

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    lineage: Mapped[str] = mapped_column(Text, default="")       # 字段级血缘（JSON 字符串）
    weknora_sync_status: Mapped[str] = mapped_column(Text, default="pending")   # pending/synced/failed
    extraction_status: Mapped[str] = mapped_column(Text, default="pending")     # pending/queued/processing/done/failed/no_entity
    # 主体是 history 固定版本时：版本引用 + 所用清洗正文的身份（正文不复制，抽取时从 history 读）
    history_source_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    history_record_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    history_version_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    history_text_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    history_text_sha256: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    history_is_demo: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    extraction_model: Mapped[str] = mapped_column(Text, default="")
    extraction_error: Mapped[str] = mapped_column(Text, default="")
    extracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_entity_count: Mapped[int] = mapped_column(Integer, default=0)   # 待审核实体数，review_lifecycle 自动维护
    total_entity_count: Mapped[int] = mapped_column(Integer, default=0)     # 文档实体总数，review_lifecycle 自动维护
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 2. doc_extra ──────────────────────────────────────────────

class DocExtra(Base):
    """来源特有字段，替代 chictr_meta / pubmed_meta / hkex_meta。"""
    __tablename__ = "doc_extra"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # 与 documents.uuid 对应
    source_site: Mapped[str] = mapped_column(Text, nullable=False, default="")
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DocAuthorityResolution(Base):
    """标准标识符对齐结果的入库记录（A1）。

    与 `doc_extra` 同级，都是 documents 的附属表（无外键，沿用软删除风格）。
    两族共用一张表，靠 `id_type` 区分：
      - `drug_name`：走 PubChem → ChEMBL → 本地白名单在线对齐，额外带
        `candidate`（实际查询的候选名）/ `rule`（命中规则）/ `candidates_tried`
      - `pmid` / `doi` / `nct_id` / `approval_number` / `cas_number`：走本地
        格式校验族（`source_id='format'`），三元组字段为空。

    `degraded=True` 表示权威库请求降级（429/5xx/网络异常），既不算命中也不算
    未命中，统计时必须单独报，不能并进未命中率。
    """
    __tablename__ = "doc_authority_resolution"
    __table_args__ = (
        UniqueConstraint("uuid", "id_type", "resolution_key", name="uq_doc_authority_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(Text, nullable=False)  # 对应 documents.uuid
    source_site: Mapped[str] = mapped_column(Text, nullable=False, default="")
    id_type: Mapped[str] = mapped_column(Text, nullable=False, default="drug_name")
    resolution_key: Mapped[str] = mapped_column(Text, nullable=False)  # 药名原值 或 字段名
    found: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 占位名（Placebo/盐水/包装描述）跳过查询，须从命中率分母剔除
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    std_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    candidate: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rule: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    candidates_tried: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_basis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    weknora_sync_status: Mapped[str] = mapped_column(Text, default="pending")
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 5. entity_aliases ─────────────────────────────────────────

class EntityAlias(Base):
    """实体别名查找表，所有名称打平到这里，消歧只需查一列。

    name_embedding 列（pgvector, vector(1024)）由 db/schema.sql 和 V001 定义。
    """
    __tablename__ = "entity_aliases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dict_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # -> entity_dict.id
    name: Mapped[str] = mapped_column(Text, nullable=False)           # 名称本身
    lang: Mapped[str] = mapped_column(Text, default="")               # zh/en/other
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=False) # 是否标准名
    entity_type: Mapped[str] = mapped_column(Text, default="")        # entity_dict.entity_type 冗余，加速查询
    name_embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(1024), nullable=True)  # 名称向量（bge-m3, 1024 维）
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


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
    source_version_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_quote: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_char_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_char_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    identity_confirmed_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    identity_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    identity_confirmation_basis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_reason: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(Text, default="pending")
    # pending / auto_approved / approved / rejected
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(Text, default="")
    weknora_graph_status: Mapped[str] = mapped_column(Text, default="pending")
    # pending / pushed / failed — WeKnora 图同步状态
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 7. entity_relations ───────────────────────────────────────

class EntityRelation(Base):
    """LLM从文档中抽取的图边，审核通过后通过WeKnora推Neo4j。"""
    __tablename__ = "entity_relations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_doc_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    source_site: Mapped[str] = mapped_column(Text, default="")
    from_entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    to_entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)
    relation_props: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_text: Mapped[str] = mapped_column(Text, default="")        # LLM抽取时的原文片段
    source_version_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_quote: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_char_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_char_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_reason: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(Text, default="pending")
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(Text, default="")
    weknora_graph_status: Mapped[str] = mapped_column(Text, default="pending")
    # pending / pushed / failed — WeKnora 图同步状态
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 8. crawl_log ──────────────────────────────────────────────

class CrawlLog(Base):
    """爬取任务日志，记录每次爬取的执行情况。"""
    __tablename__ = "crawl_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_key: Mapped[str] = mapped_column(Text, nullable=False, default="")  # nmpa/cde/pubmed/...
    task_type: Mapped[str] = mapped_column(Text, default="")           # full/incremental
    status: Mapped[str] = mapped_column(Text, default="running")       # running/success/failed
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    passed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    crawl_config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # 配置快照
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── 9. review_log ────────────────────────────────────────────

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


# ── 10. entity_merge_queue ──────────────────────────────────────

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
    existing_source_entity_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    existing_source_doc_uuid: Mapped[str] = mapped_column(Text, default="")
    resolution: Mapped[str] = mapped_column(Text, default="pending")
    resolved_value: Mapped[dict] = mapped_column(JSONB, nullable=True)
    reviewer_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 11. entity_merge_log ────────────────────────────────────────

class EntityMergeLog(Base):
    """实体合并日志：记录每次合并操作的完整快照，支持追溯和回滚。"""
    __tablename__ = "entity_merge_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    keeper_dict_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    absorbed_dict_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)

    before_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    after_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    field_decisions: Mapped[dict] = mapped_column(JSONB, default=dict)

    merge_type: Mapped[str] = mapped_column(Text, default="identity")  # identity | attribute
    reviewer: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")

    is_rolled_back: Mapped[bool] = mapped_column(Boolean, default=False)
    rolled_back_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_reason: Mapped[str] = mapped_column(Text, default="")
    rolled_back_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 12. entity_identity_merge_proposal ─────────────────────────

class EntityIdentityMergeProposal(Base):
    """身份合并提案：Agent D 判断 extracted_entity 应链接到 entity_dict，提交人工审核。"""
    __tablename__ = "entity_identity_merge_proposal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_dict_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_entity_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target_dict_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    agent_run_id: Mapped[str] = mapped_column(Text, default="")
    agent_reason: Mapped[str] = mapped_column(Text, default="")
    agent_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    match_step: Mapped[str] = mapped_column(Text, default="")
    rule_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    candidate_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True, unique=True)
    merge_log_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending | approved | rejected
    reviewer: Mapped[str] = mapped_column(Text, default="")
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 13. operation_log ──────────────────────────────────────────

class OperationLog(Base):
    """操作日志：记录所有数据库写操作的审计追踪。"""
    __tablename__ = "operation_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False, default="")    # insert/update/delete/select
    target_table: Mapped[str] = mapped_column(Text, nullable=False, default="") # 表名
    target_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True) # 受影响行 id
    business_action: Mapped[str] = mapped_column(Text, default="")              # 业务动作: merge/dedup/approve/reject/alias_sync/disambiguate
    description: Mapped[str] = mapped_column(Text, default="")                  # 一句话描述
    details: Mapped[dict] = mapped_column(JSONB, default=dict)                  # before/after 快照、变更字段等
    operator: Mapped[str] = mapped_column(Text, default="system")               # system / reviewer / disambiguation
    process: Mapped[str] = mapped_column(Text, default="")                      # 进程源: backend / crawler / worker
    trigger_source: Mapped[str] = mapped_column(Text, default="")               # 触发源函数名或 API path
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 14. agent_run ─────────────────────────────────────────────

class AgentRun(Base):
    """Agent 自动审核运行记录（无软删除、无 updated_at，与其它表不同）。"""
    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # SERIAL
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)  # entity_review | relation_review | conflict_resolution | semantic_match | entity_merge
    target_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # entity_id / relation_id / merge_id / dict_id
    status: Mapped[str] = mapped_column(String(16), default="completed")  # queued | running | completed | failed | escalated
    decision: Mapped[str] = mapped_column(String(32), default="")  # approved | rejected | keep_existing | accept_new | custom | escalated | error
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    steps: Mapped[list] = mapped_column(JSONB, default=list)  # [{step, type: ai|tool, tool_name?, content}]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    entity_name: Mapped[str] = mapped_column(String(512), default="")     # V010
    document_title: Mapped[str] = mapped_column(String(512), default="")  # V010
    document_uuid: Mapped[str] = mapped_column(String(64), default="")    # V011
    batch_id: Mapped[str] = mapped_column(String(64), default="")         # V011


class EntitySourceEvidence(Base):
    """Immutable observations. Replaying the same source material is idempotent."""
    __tablename__ = "entity_source_evidence"
    __table_args__ = (UniqueConstraint("object_type", "object_id", "evidence_key", name="uq_entity_source_evidence"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_doc_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    source_site: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    char_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    match_step: Mapped[str] = mapped_column(Text, nullable=False, default="")
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_key: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntityPrimeKGMappingProposal(Base):
    """Dictionary to PrimeKG mapping remains a proposal until explicitly accepted."""
    __tablename__ = "entity_primekg_mapping_proposal"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dict_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    native_id: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    proposal_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    reviewer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    review_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
