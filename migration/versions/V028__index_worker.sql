-- v3 3.6：索引执行与采集/发表解耦（独立 worker，db.index_publication.run_worker）。
-- lane：新增（first_seen 在 FRESH_DAYS 天内）优先、回填保底；已发出/未发出各一个部分索引，认领走索引不排序全表。
-- created_at / ready_at：入队→首次发出（first_send_at）→ready 的计时；改造前的行 created_at 为空（不知道）。
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS lane text NOT NULL DEFAULT 'backfill';
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE index_publications ALTER COLUMN created_at SET DEFAULT clock_timestamp();
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS ready_at timestamptz;
ALTER TABLE index_publications DROP CONSTRAINT IF EXISTS index_publications_lane;
ALTER TABLE index_publications ADD CONSTRAINT index_publications_lane CHECK (lane IN ('new','backfill'));
CREATE INDEX IF NOT EXISTS index_publications_due_unsent ON index_publications (lane, next_check_at, id)
  WHERE first_send_at IS NULL;
CREATE INDEX IF NOT EXISTS index_publications_due_sent ON index_publications (lane, next_check_at, id)
  WHERE first_send_at IS NOT NULL;
-- 索引分片（Codex 448/451）：太长的产物切成几片，一片一个物理条目。piece_index 0 = 不分片（身份与原来一致），
-- 1..n = 第几片；piece 是分片身份（规则版本、序号、片数、全篇绝对范围、上下文前缀长度、字数、字节数、片摘要）。
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS piece_index integer NOT NULL DEFAULT 0;
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS piece jsonb;
-- 两个唯一键都带分片序号：
-- 具体片幂等：(目标, 版本, 产物, 片)；逻辑产物复用：同一目标、同一记录、同一份清洗正文的同一片只有一个物理
-- 条目，并发入队由它保证（Codex S36-01）。改造前的待办 artifact_key 为空，只受前一个约束。
DROP INDEX IF EXISTS index_publications_artifact;
DROP INDEX IF EXISTS index_publications_record_artifact;
CREATE UNIQUE INDEX IF NOT EXISTS index_publications_artifact_piece
  ON index_publications (target_id, version_id, artifact_key, piece_index);
CREATE UNIQUE INDEX IF NOT EXISTS index_publications_record_artifact_piece
  ON index_publications (target_id, source_record_id, artifact_key, piece_index) WHERE artifact_key <> '';
