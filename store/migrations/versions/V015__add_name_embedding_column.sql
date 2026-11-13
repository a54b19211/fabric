-- V015: 修复真实 DB 缺失的 name_embedding 列与 pgvector 扩展
-- 背景：V001/V003 在部分环境未真正生效，导致 entity_aliases 缺 name_embedding 列、vector 扩展未安装。
-- 对应 db/models.py EntityAlias.name_embedding 字段（vector(1024)）。

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE entity_aliases ADD COLUMN IF NOT EXISTS name_embedding vector(1024);

CREATE INDEX IF NOT EXISTS idx_alias_embedding
    ON entity_aliases USING ivfflat (name_embedding vector_cosine_ops)
    WITH (lists = 100);
