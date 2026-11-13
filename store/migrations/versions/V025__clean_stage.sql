-- v3 3.3 清洗段的派生结果：编号（规范化后，带在派生正文里的偏移）和画像（分类、四维质量、分流）。
-- 清洗正文本身存 derived_clean_text（processor_version 见 processing.clean_text.PROCESSOR_VERSION；附件沿用 attachment_parse）。
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
-- 业务证据大类（contracts.evidence_types）：文档所属大类、可提供的大类；无法映射写 unmapped/unknown
ALTER TABLE derived_profiles ADD COLUMN IF NOT EXISTS doc_category text;
ALTER TABLE derived_profiles ADD COLUMN IF NOT EXISTS evidence_categories jsonb;
CREATE OR REPLACE RULE derived_identifiers_no_update AS ON UPDATE TO derived_identifiers DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_identifiers_no_delete AS ON DELETE TO derived_identifiers DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_profiles_no_update AS ON UPDATE TO derived_profiles DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_profiles_no_delete AS ON DELETE TO derived_profiles DO INSTEAD NOTHING;
-- 工作队列（可更新的运行状态，不是派生数据）：按（阶段键, 来源, published_seq 分块）认领，带租约。
CREATE TABLE IF NOT EXISTS derived_stage_chunks (
  stage_key text NOT NULL,
  source_id text NOT NULL,
  chunk_start bigint NOT NULL,
  chunk_end bigint NOT NULL,
  state text NOT NULL CHECK (state IN ('leased','done','failed')),
  lease_token text,
  lease_until timestamptz,
  attempts integer NOT NULL DEFAULT 0,
  stats jsonb NOT NULL DEFAULT '{}'::jsonb,
  error jsonb,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (stage_key, source_id, chunk_start),
  CHECK (chunk_end >= chunk_start)
);
CREATE INDEX IF NOT EXISTS derived_stage_chunks_state ON derived_stage_chunks (stage_key, state, lease_until);
-- 附件还没解析完：先记下，不卡住块；之后每次运行先补做
CREATE TABLE IF NOT EXISTS derived_stage_waiting (
  stage_key text NOT NULL,
  version_id char(64) NOT NULL,
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (stage_key, version_id)
);
