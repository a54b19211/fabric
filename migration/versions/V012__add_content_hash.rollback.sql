-- ============================================================
-- V012 回滚 — 移除 content_hash 列
-- ============================================================
DROP INDEX IF EXISTS idx_doc_content_hash;
ALTER TABLE documents DROP COLUMN IF EXISTS content_hash;
