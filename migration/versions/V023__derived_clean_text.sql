-- 加工层派生正文：附件等原件经处理器（processing.attachment_parse）得到的 Markdown 正文。
-- 每次尝试一行（attempt 递增），失败带原因；只追加。读取取同一处理器版本下最新一次尝试。
CREATE TABLE IF NOT EXISTS derived_clean_text (
  version_id char(64) NOT NULL REFERENCES history_versions(version_id),
  processor_version text NOT NULL,
  attempt integer NOT NULL CHECK (attempt > 0),
  source_id text NOT NULL,
  is_demo boolean NOT NULL,
  source_record_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('parsed','failed','unsupported')),
  parse_method text,
  error text,
  file_ext text,
  text text NOT NULL,
  chars integer NOT NULL,
  text_sha256 char(64) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (version_id, processor_version, attempt)
);
CREATE INDEX IF NOT EXISTS derived_clean_text_source ON derived_clean_text (source_id, is_demo, status);
CREATE OR REPLACE RULE derived_clean_text_no_update AS ON UPDATE TO derived_clean_text DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_clean_text_no_delete AS ON DELETE TO derived_clean_text DO INSTEAD NOTHING;
