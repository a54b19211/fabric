-- ============================================================
-- V001: 初始化所有表结构
-- ============================================================

-- 主文档表
CREATE TABLE IF NOT EXISTS documents (
    uuid                TEXT        PRIMARY KEY,
    title               TEXT        NOT NULL DEFAULT '',
    publish_date        TEXT        DEFAULT '',
    category            TEXT        DEFAULT '',
    summary             TEXT        DEFAULT '',
    drug_name           TEXT        DEFAULT '',
    approval_number     TEXT        DEFAULT '',
    manufacturer        TEXT        DEFAULT '',
    keywords            TEXT[]      DEFAULT '{}',
    source_url          TEXT        DEFAULT '',
    source_site         TEXT        DEFAULT '',
    local_file_path     TEXT        DEFAULT '',
    parent_uuid         TEXT        DEFAULT '',
    crawl_timestamp     TIMESTAMPTZ DEFAULT NULL,
    confidence_level    CHAR(1)     DEFAULT 'C',
    confidence_score    REAL        DEFAULT 0.3,
    confidence_reason   TEXT        DEFAULT '',
    parse_method        TEXT        DEFAULT '',
    markdown_path       TEXT        DEFAULT '',
    raw_file_path       TEXT        DEFAULT '',
    status              TEXT        DEFAULT 'AUTO_PROCESSED',
    manual_score        REAL        DEFAULT NULL,
    reviewer_note       TEXT        DEFAULT '',
    minio_bucket        TEXT        DEFAULT 'zk-data-fabric',
    minio_object_key    TEXT        DEFAULT '',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- 附件表（无外键，业务层保证关联）
CREATE TABLE IF NOT EXISTS attachments (
    id          SERIAL      PRIMARY KEY,
    parent_uuid TEXT        NOT NULL,
    file_name   TEXT        DEFAULT '',
    file_url    TEXT        DEFAULT '',
    file_uuid   TEXT        DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 港交所扩展表
CREATE TABLE IF NOT EXISTS hkex_meta (
    uuid        TEXT        PRIMARY KEY,
    stock_code  TEXT        DEFAULT '',
    stock_name  TEXT        DEFAULT '',
    stock_id    INTEGER     DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- PubMed 扩展表
CREATE TABLE IF NOT EXISTS pubmed_meta (
    uuid            TEXT    PRIMARY KEY,
    doi             TEXT    DEFAULT '',
    journal_name    TEXT    DEFAULT '',
    pmid            TEXT    DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ChiCTR 扩展表
CREATE TABLE IF NOT EXISTS chictr_meta (
    uuid                TEXT    PRIMARY KEY,
    regno               TEXT    DEFAULT '',
    reg_status          TEXT    DEFAULT '',
    study_type          TEXT    DEFAULT '',
    study_stage         TEXT    DEFAULT '',
    intervention        TEXT    DEFAULT '',
    condition           TEXT    DEFAULT '',
    sample_size         TEXT    DEFAULT '',
    recruitment_status  TEXT    DEFAULT '',
    primary_sponsor     TEXT    DEFAULT '',
    ethics_approved     TEXT    DEFAULT '',
    ethics_committee    TEXT    DEFAULT '',
    ethics_date         TEXT    DEFAULT '',
    start_date          TEXT    DEFAULT '',
    end_date            TEXT    DEFAULT '',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_documents_source_site   ON documents(source_site);
CREATE INDEX IF NOT EXISTS idx_documents_publish_date  ON documents(publish_date);
CREATE INDEX IF NOT EXISTS idx_documents_status        ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_drug_name     ON documents(drug_name);
CREATE INDEX IF NOT EXISTS idx_documents_parent_uuid   ON documents(parent_uuid);
CREATE INDEX IF NOT EXISTS idx_attachments_parent_uuid ON attachments(parent_uuid);
CREATE INDEX IF NOT EXISTS idx_hkex_meta_stock_code    ON hkex_meta(stock_code);
CREATE INDEX IF NOT EXISTS idx_chictr_meta_regno       ON chictr_meta(regno);
CREATE INDEX IF NOT EXISTS idx_pubmed_meta_doi         ON pubmed_meta(doi);

