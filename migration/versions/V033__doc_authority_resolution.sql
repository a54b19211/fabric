-- 标准标识符对齐结果的正式落库表（A1）。
-- 此前对齐结果只写在 data/authority/<site>/<uuid>.json 旁路文件里，数据库无记录，
-- 论文所称「标准标识符对齐」在存量语料上没有可复查的入库结果。本表承接原链路
-- storage_writer.write_to_database 的双写，文件旁路继续保留（可复查快照）。
-- 两族共用一张表，用 id_type 区分：drug_name 走 PubChem/ChEMBL 在线对齐，
-- pmid/doi/nct_id/approval_number/cas_number 走本地格式校验族（format）。
CREATE TABLE IF NOT EXISTS doc_authority_resolution (
    id               BIGSERIAL   PRIMARY KEY,
    uuid             TEXT        NOT NULL,                 -- 对应 documents.uuid（无外键，沿用本项目软删除风格）
    source_site      TEXT        NOT NULL DEFAULT '',
    id_type          TEXT        NOT NULL DEFAULT 'drug_name',
                                 -- drug_name / pmid / doi / nct_id / approval_number / cas_number
    resolution_key   TEXT        NOT NULL,                 -- 药名族为原始药名，标识符族为字段名（如 pmid）
    found            BOOLEAN     NOT NULL DEFAULT FALSE,   -- 是否命中权威库/通过格式校验
    degraded         BOOLEAN     NOT NULL DEFAULT FALSE,   -- 降级（429/5xx/网络异常），不计入未命中
    std_id           TEXT,                                 -- PUBCHEM:148124 / CHEMBL:... / PMID:... / DOI:...
    source_id        TEXT,                                 -- pubchem / chembl / format / whitelist
    raw_value        TEXT,                                 -- 归一前的原始值
    resolved_value   TEXT,                                 -- 权威库返回的规范值（可能很长，TEXT 足够）
    candidate        TEXT,                                 -- 药名归一实际用于查询的候选名（A0.5）
    rule             TEXT,                                 -- as_is/cut_descriptor/inner_alias/comma_segment/combo_split
    candidates_tried INTEGER,                              -- 请求成本：试过几个候选名
    confidence       DOUBLE PRECISION,
    candidates       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    is_deleted       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 同一文档同一族同一键只留一条；写入用「先删后插」保证幂等
    UNIQUE (uuid, id_type, resolution_key)
);

CREATE INDEX IF NOT EXISTS idx_doc_authority_uuid      ON doc_authority_resolution(uuid);
CREATE INDEX IF NOT EXISTS idx_doc_authority_site      ON doc_authority_resolution(source_site);
CREATE INDEX IF NOT EXISTS idx_doc_authority_id_type   ON doc_authority_resolution(id_type);
CREATE INDEX IF NOT EXISTS idx_doc_authority_std_id    ON doc_authority_resolution(std_id);
CREATE INDEX IF NOT EXISTS idx_doc_authority_deleted   ON doc_authority_resolution(is_deleted);
-- 命中率统计只关心 found=true 的行，用部分索引避免全表扫
CREATE INDEX IF NOT EXISTS idx_doc_authority_matched   ON doc_authority_resolution(uuid) WHERE found;
-- 降级行单独索引：降级率是论文里必须单独报的口径，不能混进未命中
CREATE INDEX IF NOT EXISTS idx_doc_authority_degraded  ON doc_authority_resolution(uuid) WHERE degraded;
