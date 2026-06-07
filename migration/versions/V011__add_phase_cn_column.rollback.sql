-- ============================================================
-- V011 回滚 — 移除 phase_cn 列
-- ============================================================
DROP INDEX IF EXISTS idx_dict_phase_cn;
ALTER TABLE entity_dict DROP COLUMN IF EXISTS phase_cn;
