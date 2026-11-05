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
