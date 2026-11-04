-- v3 3.3 清洗段的派生结果：编号（规范化后，带在派生正文里的偏移）和画像（分类、四维质量、分流）。
-- 清洗正文本身存 derived_clean_text（processor_version=datafabric.clean_text.v1；附件沿用 attachment_parse）。
-- 只追加；任何一个处理器（清洗、附件解析、编号、画像）升级都写新行，旧行保留。
CREATE TABLE IF NOT EXISTS derived_identifiers (
  version_id char(64) NOT NULL REFERENCES history_versions(version_id),
  processor_version text NOT NULL,
  text_processor_version text NOT NULL,
  kind text NOT NULL,
  value text NOT NULL,
  start_offset integer NOT NULL,
  end_offset integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (version_id, processor_version, text_processor_version, kind, value, start_offset)
);
CREATE INDEX IF NOT EXISTS derived_identifiers_value ON derived_identifiers (kind, value);
CREATE TABLE IF NOT EXISTS derived_profiles (
  version_id char(64) NOT NULL REFERENCES history_versions(version_id),
  processor_version text NOT NULL,
  text_processor_version text NOT NULL,
  source_id text NOT NULL,
  is_demo boolean NOT NULL,
  doc_type text NOT NULL,
  evidence_types jsonb NOT NULL,
  decisions jsonb NOT NULL,
  quality jsonb NOT NULL,
  route text NOT NULL CHECK (route IN ('normal','weak_signal','quarantine')),
  route_reasons jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  -- 按“基于哪一版清洗正文”区分：清洗器或附件解析器升级后，编号和画像会在新正文上重算，旧行保留
  PRIMARY KEY (version_id, processor_version, text_processor_version)
);
CREATE INDEX IF NOT EXISTS derived_profiles_route ON derived_profiles (source_id, is_demo, route);
CREATE OR REPLACE RULE derived_identifiers_no_update AS ON UPDATE TO derived_identifiers DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_identifiers_no_delete AS ON DELETE TO derived_identifiers DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_profiles_no_update AS ON UPDATE TO derived_profiles DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_profiles_no_delete AS ON DELETE TO derived_profiles DO INSTEAD NOTHING;
