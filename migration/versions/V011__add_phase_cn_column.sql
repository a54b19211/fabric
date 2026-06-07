-- ============================================================
-- V011 — entity_dict 添加 phase_cn 结构化列
-- 从 canonical_data JSONB 中提取高频查询字段
-- ============================================================
ALTER TABLE entity_dict ADD COLUMN IF NOT EXISTS phase_cn TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_dict_phase_cn ON entity_dict(phase_cn);
