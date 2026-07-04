-- ============================================================
-- V002 — 补充旧版 schema 缺失的字段
-- 适用于：数据库由 V001 之前的版本建立，缺少部分字段
-- ============================================================

-- documents 补充 raw_content 字段（如果也缺的话）
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS raw_content TEXT DEFAULT '';

-- attachments 补充 local_path 字段
ALTER TABLE attachments
    ADD COLUMN IF NOT EXISTS local_path TEXT DEFAULT '';

-- extracted_entities 补充 source_text 字段
ALTER TABLE extracted_entities
    ADD COLUMN IF NOT EXISTS source_text TEXT DEFAULT '';
