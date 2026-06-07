-- ============================================================
-- V010 回滚 — 移除复合索引
-- ============================================================
DROP INDEX IF EXISTS idx_ent_review_deleted;
DROP INDEX IF EXISTS idx_ent_weknora_review;
DROP INDEX IF EXISTS idx_rel_review_deleted;
DROP INDEX IF EXISTS idx_rel_weknora_review;
DROP INDEX IF EXISTS idx_doc_extract_deleted;
