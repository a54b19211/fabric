-- V003 — 将 entity_aliases.name_embedding 维度从 1536 改为 1024
-- 原因：实际使用 DashScope text-embedding-v3（1024维），schema 原来写的是 OpenAI ada-002（1536维）

-- 1. 删除旧的 ivfflat 索引
DROP INDEX IF EXISTS idx_alias_embedding;

-- 2. 清空旧的 embedding 数据（维度不兼容，必须重建）
UPDATE entity_aliases SET name_embedding = NULL;

-- 3. 修改列类型为 vector(1024)
ALTER TABLE entity_aliases
    ALTER COLUMN name_embedding TYPE vector(1024);

-- 4. 重建 ivfflat 索引
CREATE INDEX IF NOT EXISTS idx_alias_embedding
    ON entity_aliases USING ivfflat (name_embedding vector_cosine_ops)
    WITH (lists = 100);
