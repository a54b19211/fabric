-- ============================================================
-- V001 — 初始化完整 Schema
-- ZK_Data Fabric PostgreSQL Schema v1
-- 9张表，与 db/models.py 严格对齐
-- ============================================================

-- pgvector 扩展（实体消歧向量检索用）
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- 1. documents — 原始文档
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    id                   BIGSERIAL   PRIMARY KEY,
    uuid                 TEXT        NOT NULL UNIQUE,
    title                TEXT        NOT NULL DEFAULT '',
    publish_date         TEXT        DEFAULT '',
    category             TEXT        DEFAULT '',
    summary              TEXT        DEFAULT '',
    raw_content          TEXT        DEFAULT '',
    source_url           TEXT        DEFAULT '',
    source_site          TEXT        DEFAULT '',
    parent_uuid          TEXT        DEFAULT '',
    crawl_timestamp      TIMESTAMPTZ DEFAULT NULL,
    confidence_score     DOUBLE PRECISION DEFAULT 0.3,
    confidence_reason    TEXT        DEFAULT '',
    parse_method         TEXT        DEFAULT '',
    doc_status           TEXT        DEFAULT 'AUTO_PROCESSED',
    reviewer_note        TEXT        DEFAULT '',
    doc_type             TEXT        DEFAULT '',
    language             TEXT        DEFAULT '',
    weknora_sync_status  TEXT        DEFAULT 'pending',
    extraction_status    TEXT        DEFAULT 'pending',
    extraction_model     TEXT        DEFAULT '',
    extraction_error     TEXT        DEFAULT '',
    extracted_at         TIMESTAMPTZ DEFAULT NULL,
    pending_entity_count INT         DEFAULT 0,
    total_entity_count   INT         DEFAULT 0,
    is_deleted           BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_doc_uuid              ON documents(uuid);
CREATE INDEX IF NOT EXISTS idx_doc_source_site       ON documents(source_site);
CREATE INDEX IF NOT EXISTS idx_doc_publish_date      ON documents(publish_date);
CREATE INDEX IF NOT EXISTS idx_doc_doc_status        ON documents(doc_status);
CREATE INDEX IF NOT EXISTS idx_doc_parent_uuid       ON documents(parent_uuid);
CREATE INDEX IF NOT EXISTS idx_doc_extraction_status ON documents(extraction_status);
CREATE INDEX IF NOT EXISTS idx_doc_weknora_sync      ON documents(weknora_sync_status);
CREATE INDEX IF NOT EXISTS idx_doc_is_deleted        ON documents(is_deleted);

-- ============================================================
-- 2. doc_extra — 来源特有字段
-- ============================================================
CREATE TABLE IF NOT EXISTS doc_extra (
    id          BIGSERIAL   PRIMARY KEY,
    uuid        TEXT        NOT NULL UNIQUE,
    source_site TEXT        NOT NULL DEFAULT '',
    extra       JSONB       NOT NULL DEFAULT '{}',
    is_deleted  BOOLEAN     NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_doc_extra_uuid    ON doc_extra(uuid);
CREATE INDEX IF NOT EXISTS idx_doc_extra_site    ON doc_extra(source_site);
CREATE INDEX IF NOT EXISTS idx_doc_extra_jsonb   ON doc_extra USING GIN (extra);
CREATE INDEX IF NOT EXISTS idx_doc_extra_deleted ON doc_extra(is_deleted);

-- ============================================================
-- 3. attachments — 文档附件
-- ============================================================
CREATE TABLE IF NOT EXISTS attachments (
    id          BIGSERIAL   PRIMARY KEY,
    parent_uuid TEXT        NOT NULL,
    file_name   TEXT        DEFAULT '',
    file_url    TEXT        DEFAULT '',
    local_path  TEXT        DEFAULT '',
    file_uuid   TEXT        DEFAULT '',
    is_deleted  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_att_parent_uuid ON attachments(parent_uuid);
CREATE INDEX IF NOT EXISTS idx_att_is_deleted  ON attachments(is_deleted);

-- ============================================================
-- 4. entity_dict — 实体字典（消歧主表）
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_dict (
    id                   BIGSERIAL   PRIMARY KEY,
    entity_type          TEXT        NOT NULL,
    external_ids         JSONB       NOT NULL DEFAULT '{}',
    canonical_data       JSONB       NOT NULL DEFAULT '{}',
    canonical_source     TEXT        DEFAULT '',
    canonical_updated_at TIMESTAMPTZ DEFAULT NULL,
    is_verified          BOOLEAN     DEFAULT FALSE,
    weknora_sync_status  TEXT        DEFAULT 'pending',
    is_deleted           BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dict_entity_type   ON entity_dict(entity_type);
CREATE INDEX IF NOT EXISTS idx_dict_external_ids  ON entity_dict USING GIN (external_ids);
CREATE INDEX IF NOT EXISTS idx_dict_is_deleted    ON entity_dict(is_deleted);
CREATE INDEX IF NOT EXISTS idx_dict_weknora_sync  ON entity_dict(weknora_sync_status);

-- ============================================================
-- 5. entity_aliases — 实体别名查找表（含向量字段）
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_aliases (
    id             BIGSERIAL   PRIMARY KEY,
    dict_id        BIGINT      NOT NULL,
    name           TEXT        NOT NULL,
    lang           TEXT        DEFAULT '',
    is_canonical   BOOLEAN     DEFAULT FALSE,
    entity_type    TEXT        DEFAULT '',
    name_embedding vector(1536),
    is_deleted     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_name_dict ON entity_aliases(name, dict_id);
CREATE INDEX IF NOT EXISTS idx_alias_name             ON entity_aliases(name);
CREATE INDEX IF NOT EXISTS idx_alias_dict_id          ON entity_aliases(dict_id);
CREATE INDEX IF NOT EXISTS idx_alias_is_deleted       ON entity_aliases(is_deleted);
CREATE INDEX IF NOT EXISTS idx_alias_entity_type      ON entity_aliases(entity_type);
CREATE INDEX IF NOT EXISTS idx_alias_embedding
    ON entity_aliases USING ivfflat (name_embedding vector_cosine_ops)
    WITH (lists = 100);

-- ============================================================
-- 6. extracted_entities — LLM抽取的图节点暂存
-- ============================================================
CREATE TABLE IF NOT EXISTS extracted_entities (
    id                BIGSERIAL   PRIMARY KEY,
    source_doc_uuid   TEXT        NOT NULL,
    source_site       TEXT        DEFAULT '',
    entity_type       TEXT        NOT NULL,
    entity_name       TEXT        NOT NULL DEFAULT '',
    entity_data       JSONB       NOT NULL DEFAULT '{}',
    source_text       TEXT        DEFAULT '',
    dict_id           BIGINT      DEFAULT NULL,
    confidence_score  DOUBLE PRECISION DEFAULT 0.0,
    confidence_reason TEXT        DEFAULT '',
    review_status     TEXT        DEFAULT 'pending',
    reviewer_note     TEXT        DEFAULT '',
    reviewed_at       TIMESTAMPTZ DEFAULT NULL,
    reviewed_by       TEXT        DEFAULT '',
    weknora_graph_status TEXT     DEFAULT 'pending',
    is_deleted        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ent_source_doc    ON extracted_entities(source_doc_uuid);
CREATE INDEX IF NOT EXISTS idx_ent_entity_type   ON extracted_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_ent_dict_id       ON extracted_entities(dict_id);
CREATE INDEX IF NOT EXISTS idx_ent_review_status ON extracted_entities(review_status);
CREATE INDEX IF NOT EXISTS idx_ent_data_jsonb    ON extracted_entities USING GIN (entity_data);
CREATE INDEX IF NOT EXISTS idx_ent_is_deleted    ON extracted_entities(is_deleted);
CREATE INDEX IF NOT EXISTS idx_ent_weknora_graph ON extracted_entities(weknora_graph_status);

-- ============================================================
-- 7. entity_relations — LLM抽取的图边暂存
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_relations (
    id                BIGSERIAL   PRIMARY KEY,
    source_doc_uuid   TEXT        NOT NULL,
    source_site       TEXT        DEFAULT '',
    from_entity_id    BIGINT      DEFAULT NULL,
    to_entity_id      BIGINT      DEFAULT NULL,
    relation_type     TEXT        NOT NULL,
    relation_props    JSONB       NOT NULL DEFAULT '{}',
    source_text       TEXT        DEFAULT '',
    confidence_score  DOUBLE PRECISION DEFAULT 0.0,
    confidence_reason TEXT        DEFAULT '',
    review_status     TEXT        DEFAULT 'pending',
    reviewer_note     TEXT        DEFAULT '',
    reviewed_at       TIMESTAMPTZ DEFAULT NULL,
    reviewed_by       TEXT        DEFAULT '',
    weknora_graph_status TEXT     DEFAULT 'pending',
    is_deleted        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rel_source_doc    ON entity_relations(source_doc_uuid);
CREATE INDEX IF NOT EXISTS idx_rel_from_entity   ON entity_relations(from_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_to_entity     ON entity_relations(to_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_relation_type ON entity_relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_rel_review_status ON entity_relations(review_status);
CREATE INDEX IF NOT EXISTS idx_rel_is_deleted    ON entity_relations(is_deleted);
CREATE INDEX IF NOT EXISTS idx_rel_weknora_graph ON entity_relations(weknora_graph_status);

-- ============================================================
-- 8. crawl_log — 爬取任务日志
-- ============================================================
CREATE TABLE IF NOT EXISTS crawl_log (
    id                    BIGSERIAL   PRIMARY KEY,
    site_key              TEXT        NOT NULL DEFAULT '',
    task_type             TEXT        DEFAULT '',
    status                TEXT        DEFAULT 'running',
    total_count           INTEGER     DEFAULT 0,
    passed_count          INTEGER     DEFAULT 0,
    failed_count          INTEGER     DEFAULT 0,
    error_message         TEXT        DEFAULT '',
    crawl_config_snapshot JSONB       DEFAULT '{}',
    started_at            TIMESTAMPTZ DEFAULT NOW(),
    finished_at           TIMESTAMPTZ DEFAULT NULL,
    is_deleted            BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crawl_site_key   ON crawl_log(site_key);
CREATE INDEX IF NOT EXISTS idx_crawl_status     ON crawl_log(status);
CREATE INDEX IF NOT EXISTS idx_crawl_is_deleted ON crawl_log(is_deleted);

-- ============================================================
-- 9. review_log — 审核操作日志
-- ============================================================
CREATE TABLE IF NOT EXISTS review_log (
    id          BIGSERIAL   PRIMARY KEY,
    object_type TEXT        NOT NULL DEFAULT '',
    object_id   BIGINT      NOT NULL,
    action      TEXT        NOT NULL DEFAULT '',
    reviewer    TEXT        DEFAULT '',
    note        TEXT        DEFAULT '',
    before_data JSONB       DEFAULT '{}',
    after_data  JSONB       DEFAULT '{}',
    is_deleted  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_review_object     ON review_log(object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_review_is_deleted ON review_log(is_deleted);
