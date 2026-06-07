-- ============================================================
-- V012 — documents 添加 content_hash 列
-- 用于跨源去重检测
-- ============================================================
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_doc_content_hash ON documents(content_hash);
