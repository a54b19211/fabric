-- V004 — 实体数据合并队列
-- 审核通过后，自动合并抽取数据到 entity_dict.canonical_data。
-- auto 的规则处理；同优先级不同源的值差异进队列人工审核。

CREATE TABLE IF NOT EXISTS entity_merge_queue (
    id               BIGSERIAL    PRIMARY KEY,
    source_entity_id BIGINT       NOT NULL,    -- extracted_entities.id
    target_dict_id   BIGINT       NOT NULL,    -- entity_dict.id
    field_name       TEXT         NOT NULL,    -- 冲突字段名
    existing_value   JSONB,                    -- entity_dict 当前值
    new_value        JSONB,                    -- extracted_entity 带来的值
    source_site      TEXT         DEFAULT '',  -- 新值来源站点
    resolution       TEXT         DEFAULT 'pending',
    -- pending / auto_merged / accept_new / keep_existing / custom
    resolved_value   JSONB,                    -- 最终采用的值（custom 时填）
    reviewer_note    TEXT         DEFAULT '',
    reviewed_by      TEXT         DEFAULT '',
    reviewed_at      TIMESTAMPTZ  DEFAULT NULL,
    created_at       TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merge_queue_resolution ON entity_merge_queue(resolution);
CREATE INDEX IF NOT EXISTS idx_merge_queue_dict_id    ON entity_merge_queue(target_dict_id);
