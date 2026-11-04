-- 索引作业管理页按更新时间倒序续页（db/index_publication.status：ORDER BY updated_at DESC,id DESC，
-- 续页条件 (updated_at,id)<(:at,:id)）。与分页顺序一致的索引，避免全表排序（Codex 400 第 6 点）。
CREATE INDEX IF NOT EXISTS index_publications_recent ON index_publications (updated_at DESC, id DESC);
-- 按状态过滤的续页（受阻、处理中等少数状态）走状态前缀；来源过滤经目标表连接，沿用上一条（EXPLAIN 见 T-28 r2）。
CREATE INDEX IF NOT EXISTS index_publications_state_recent ON index_publications (state, updated_at DESC, id DESC);
