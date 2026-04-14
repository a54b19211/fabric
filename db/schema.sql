-- ============================================================
-- ZK_Data Fabric — PostgreSQL 表结构
-- ============================================================

-- ── 主文档表（所有字段对所有来源通用）──
CREATE TABLE IF NOT EXISTS documents (
    uuid                TEXT        PRIMARY KEY,

    -- 基本信息
    title               TEXT        NOT NULL DEFAULT '',
    publish_date        TEXT        DEFAULT '',      -- YYYY-MM-DD，各来源格式不统一故用 TEXT
    category            TEXT        DEFAULT '',
    summary             TEXT        DEFAULT '',

    -- 药学通用业务字段
    drug_name           TEXT        DEFAULT '',
    approval_number     TEXT        DEFAULT '',
    manufacturer        TEXT        DEFAULT '',
    keywords            TEXT[]      DEFAULT '{}',

    -- 来源血缘
    source_url          TEXT        DEFAULT '',
    source_site         TEXT        DEFAULT '',      -- nmpa / cde / chictr / pubmed / hkex ...
    local_file_path     TEXT        DEFAULT '',
    parent_uuid         TEXT        DEFAULT '',      -- 附件 PDF 指向父文档，业务层保证关联
    crawl_timestamp     TIMESTAMPTZ DEFAULT NULL,

    -- 置信度
    confidence_level    CHAR(1)     DEFAULT 'C',     -- A / B / C
    confidence_score    REAL        DEFAULT 0.3,
    confidence_reason   TEXT        DEFAULT '',

    -- 解析元信息
    parse_method        TEXT        DEFAULT '',
    markdown_path       TEXT        DEFAULT '',
    raw_file_path       TEXT        DEFAULT '',

    -- 审核状态
    status              TEXT        DEFAULT 'AUTO_PROCESSED',
    manual_score        REAL        DEFAULT NULL,
    reviewer_note       TEXT        DEFAULT '',

    -- MinIO 预留（二期）
    minio_bucket        TEXT        DEFAULT 'zk-data-fabric',
    minio_object_key    TEXT        DEFAULT '',

    -- 系统字段
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ── 附件表（业务层通过 parent_uuid 自行保证关联，无外键约束）──
CREATE TABLE IF NOT EXISTS attachments (
    id          SERIAL      PRIMARY KEY,
    parent_uuid TEXT        NOT NULL,
    file_name   TEXT        DEFAULT '',
    file_url    TEXT        DEFAULT '',
    file_uuid   TEXT        DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── 港交所扩展表（仅 hkex 来源写入，通过 uuid 与 documents 关联）──
CREATE TABLE IF NOT EXISTS hkex_meta (
    uuid        TEXT        PRIMARY KEY,
    stock_code  TEXT        DEFAULT '',
    stock_name  TEXT        DEFAULT '',
    stock_id    INTEGER     DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── PubMed 扩展表（仅 pubmed 来源写入）──
CREATE TABLE IF NOT EXISTS pubmed_meta (
    uuid            TEXT    PRIMARY KEY,
    doi             TEXT    DEFAULT '',
    journal_name    TEXT    DEFAULT '',
    pmid            TEXT    DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── ChiCTR 扩展表（仅 chictr 来源写入）──
CREATE TABLE IF NOT EXISTS chictr_meta (
    uuid                TEXT    PRIMARY KEY,
    regno               TEXT    DEFAULT '',   -- ChiCTR 注册号
    reg_status          TEXT    DEFAULT '',   -- 预注册 / 补注册
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

-- ── 索引 ──
CREATE INDEX IF NOT EXISTS idx_documents_source_site   ON documents(source_site);
CREATE INDEX IF NOT EXISTS idx_documents_publish_date  ON documents(publish_date);
CREATE INDEX IF NOT EXISTS idx_documents_status        ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_drug_name     ON documents(drug_name);
CREATE INDEX IF NOT EXISTS idx_documents_parent_uuid   ON documents(parent_uuid);
CREATE INDEX IF NOT EXISTS idx_attachments_parent_uuid ON attachments(parent_uuid);
CREATE INDEX IF NOT EXISTS idx_hkex_meta_stock_code    ON hkex_meta(stock_code);
CREATE INDEX IF NOT EXISTS idx_chictr_meta_regno       ON chictr_meta(regno);
CREATE INDEX IF NOT EXISTS idx_pubmed_meta_doi         ON pubmed_meta(doi);

