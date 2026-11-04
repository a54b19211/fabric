-- 加工层第一张派生表：临床登记前后两个固定版本的逐字段差分（processing.registry_diff）。
-- 与原件分开存，原件表不改。每个被处理过的新版本在 derived_registry_diff_runs 留一行（含“无变化”和
-- “首次采用、没有上一版”），所以“没处理”和“处理了但没变化”能区分开。只追加，处理器升级写新版本行。
CREATE TABLE IF NOT EXISTS derived_registry_diff_runs (
  to_version_id char(64) NOT NULL REFERENCES history_versions(version_id),
  processor_version text NOT NULL,
  source_id text NOT NULL,
  is_demo boolean NOT NULL,
  source_record_id text NOT NULL,
  from_version_id char(64) REFERENCES history_versions(version_id),
  pairing text NOT NULL,
  change_count integer NOT NULL,
  material_count integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (to_version_id, processor_version)
);
CREATE TABLE IF NOT EXISTS derived_registry_changes (
  id char(64) PRIMARY KEY,
  processor_version text NOT NULL,
  source_id text NOT NULL,
  is_demo boolean NOT NULL,
  source_record_id text NOT NULL,
  from_version_id char(64) NOT NULL REFERENCES history_versions(version_id),
  to_version_id char(64) NOT NULL REFERENCES history_versions(version_id),
  field text NOT NULL,
  change_kind text NOT NULL,
  json_pointer text,
  old_value jsonb NOT NULL,
  new_value jsonb NOT NULL,
  detail jsonb NOT NULL,
  material boolean NOT NULL,
  material_reason text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS derived_registry_changes_to ON derived_registry_changes (to_version_id, processor_version);
CREATE INDEX IF NOT EXISTS derived_registry_changes_kind ON derived_registry_changes
  (source_id, is_demo, change_kind, created_at);
CREATE OR REPLACE RULE derived_registry_diff_runs_no_update AS ON UPDATE TO derived_registry_diff_runs DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_registry_diff_runs_no_delete AS ON DELETE TO derived_registry_diff_runs DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_registry_changes_no_update AS ON UPDATE TO derived_registry_changes DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_registry_changes_no_delete AS ON DELETE TO derived_registry_changes DO INSTEAD NOTHING;
