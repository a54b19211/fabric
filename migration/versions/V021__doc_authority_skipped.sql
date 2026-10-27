-- 补 skipped 标记：占位名（Placebo / 盐水 / 产品包装描述）不送权威库，
-- 必须从命中率分母里剔除。没有该列时只能靠 candidates_tried=0 推断，
-- 属于隐式约定；显式存列后命中率口径可直接用 SQL 复算：
--   eligible = count(*) filter (where not skipped)
--   match_rate = matched / eligible
ALTER TABLE doc_authority_resolution
    ADD COLUMN IF NOT EXISTS skipped BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_doc_authority_skipped
    ON doc_authority_resolution(uuid) WHERE skipped;
