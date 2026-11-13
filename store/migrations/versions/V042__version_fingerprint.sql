-- 版本逻辑改成"内容指纹 + 上一版"（store/history.py _publish）：
--   fingerprint  原件的内容指纹（store.history.fingerprint，Python 算；旧版本由 ops/backfill_fingerprint.py 补，
--                没补的在比较时当场算）
--   replaced_by  被哪个新版本取代；为空的就是这条记录现在的版本
-- 原件仍只追加：版本行只允许把这两列从空补上一次，别的列改不了、行删不了。
ALTER TABLE history_versions ADD COLUMN IF NOT EXISTS fingerprint char(64);
ALTER TABLE history_versions ADD COLUMN IF NOT EXISTS replaced_by char(64);

DROP RULE IF EXISTS history_versions_no_update ON history_versions;
CREATE OR REPLACE FUNCTION history_versions_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.version_id, NEW.source_id, NEW.is_demo, NEW.source_record_id, NEW.record_kind, NEW.first_seen_at,
      NEW.received_at, NEW.proof_available_at, NEW.published_seq, NEW.payload)
     IS DISTINCT FROM
     (OLD.version_id, OLD.source_id, OLD.is_demo, OLD.source_record_id, OLD.record_kind, OLD.first_seen_at,
      OLD.received_at, OLD.proof_available_at, OLD.published_seq, OLD.payload) THEN
    RAISE EXCEPTION 'history_versions is append-only: only fingerprint and replaced_by may be filled in';
  END IF;
  IF (OLD.fingerprint IS NOT NULL AND NEW.fingerprint IS DISTINCT FROM OLD.fingerprint)
     OR (OLD.replaced_by IS NOT NULL AND NEW.replaced_by IS DISTINCT FROM OLD.replaced_by) THEN
    RAISE EXCEPTION 'history_versions: fingerprint and replaced_by are filled in once, never changed';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS history_versions_append_only ON history_versions;
CREATE TRIGGER history_versions_append_only BEFORE UPDATE ON history_versions
  FOR EACH ROW EXECUTE FUNCTION history_versions_append_only();

-- 旧版本的上一版/下一版：同一条记录（同一身份：来源自己的 / legacy 迁入的）按发布序号排，前一个指向后一个
UPDATE history_versions h SET replaced_by = n.next_id
FROM (SELECT version_id, lead(version_id) OVER (
        PARTITION BY source_id, is_demo, source_record_id,
                     COALESCE(payload->'facts'->>'publication_role', 'source_record')
        ORDER BY published_seq) AS next_id
      FROM history_versions) n
WHERE h.version_id = n.version_id AND n.next_id IS NOT NULL AND h.replaced_by IS NULL;

CREATE INDEX IF NOT EXISTS history_versions_current ON history_versions (source_id, is_demo, source_record_id)
  WHERE replaced_by IS NULL;
CREATE INDEX IF NOT EXISTS history_versions_replaced_by ON history_versions (replaced_by)
  WHERE replaced_by IS NOT NULL;

-- 采集任务不再比对请求哈希
ALTER TABLE history_runs ALTER COLUMN request_hash DROP NOT NULL;
