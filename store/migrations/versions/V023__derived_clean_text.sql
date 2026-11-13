-- 加工层派生正文：附件等原件经处理器（processing.attachment_parse）得到的正文和定位。
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
  parse_attempts jsonb NOT NULL DEFAULT '[]'::jsonb,
  error text,
  file_ext text,
  text text NOT NULL,
  chars integer NOT NULL,
  text_sha256 char(64) NOT NULL,
  -- 定位：pdf_page / converted_page / block / sheet / unavailable；segments 是每块在正文里的偏移和定位
  locator_kind text,
  segments jsonb NOT NULL DEFAULT '[]'::jsonb,
  language text,
  warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (version_id, processor_version, attempt)
);
-- 早先只建了基础列的库（未上线的测试库）补齐后加的列；新库这几句什么也不做
ALTER TABLE derived_clean_text ADD COLUMN IF NOT EXISTS parse_attempts jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE derived_clean_text ADD COLUMN IF NOT EXISTS locator_kind text;
ALTER TABLE derived_clean_text ADD COLUMN IF NOT EXISTS segments jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE derived_clean_text ADD COLUMN IF NOT EXISTS language text;
ALTER TABLE derived_clean_text ADD COLUMN IF NOT EXISTS warnings jsonb NOT NULL DEFAULT '[]'::jsonb;
-- 清洗器输入的摘要：同一清洗器版本下见过的输入直接复用结果（v3 3.3“原文没变就复用”）
ALTER TABLE derived_clean_text ADD COLUMN IF NOT EXISTS input_sha256 char(64);
CREATE INDEX IF NOT EXISTS derived_clean_text_input ON derived_clean_text (input_sha256, processor_version);
CREATE INDEX IF NOT EXISTS derived_clean_text_source ON derived_clean_text (source_id, is_demo, status);
CREATE OR REPLACE RULE derived_clean_text_no_update AS ON UPDATE TO derived_clean_text DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_clean_text_no_delete AS ON DELETE TO derived_clean_text DO INSTEAD NOTHING;
-- 处理认领：同一版本同一处理器版本同时只有一个持有者；持有者中断，租约过期后可被重新认领。
CREATE TABLE IF NOT EXISTS derived_text_claims (
  version_id char(64) NOT NULL REFERENCES history_versions(version_id),
  processor_version text NOT NULL,
  lease_token text NOT NULL,
  lease_until timestamptz NOT NULL,
  PRIMARY KEY (version_id, processor_version)
);
-- 附件与父页面的固定版本关系（attachment_of）。同一附件版本可先后挂在多个父版本下，每次都留一行。
-- 父版本未知（例如按旧库链接补抓，父页面没有重抓）时 parent_version_id 为空，link_basis 写明原因。
CREATE TABLE IF NOT EXISTS history_attachment_links (
  attachment_version_id char(64) NOT NULL REFERENCES history_versions(version_id),
  parent_record_id text NOT NULL,
  parent_version_id char(64) REFERENCES history_versions(version_id),
  link_basis text NOT NULL,
  run_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX IF NOT EXISTS history_attachment_links_once ON history_attachment_links
  (attachment_version_id, parent_record_id, COALESCE(parent_version_id, ''));
CREATE OR REPLACE RULE history_attachment_links_no_update AS ON UPDATE TO history_attachment_links DO INSTEAD NOTHING;
CREATE OR REPLACE RULE history_attachment_links_no_delete AS ON DELETE TO history_attachment_links DO INSTEAD NOTHING;
