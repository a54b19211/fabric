-- ============================================================
-- V002: 新增 RAG 相关字段
--   documents:   doc_type, language, weknora_sync_status
--   pubmed_meta: pmcid, fulltext_status
-- ============================================================

-- documents 表新增三列
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS doc_type             TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS language             TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS weknora_sync_status  TEXT DEFAULT 'pending';

-- doc_type 索引（按文档类型过滤是高频查询）
CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);

-- weknora_sync_status 索引（用于查询待同步/同步失败的记录）
CREATE INDEX IF NOT EXISTS idx_documents_weknora_sync_status ON documents(weknora_sync_status);

-- pubmed_meta 表新增两列
ALTER TABLE pubmed_meta
    ADD COLUMN IF NOT EXISTS pmcid           TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS fulltext_status TEXT DEFAULT ''
