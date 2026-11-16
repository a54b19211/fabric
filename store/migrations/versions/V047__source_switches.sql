-- 来源开关进库，经 /api/fabric/admin/sources 读改（store/source_admin.py）：
-- fetch_enabled：定时刷新（fetch/source_refresh.py）抓不抓这个来源，默认开（和以前一样，配置了 watch 就抓）。
-- fabric_source_switch_log：每次改开关记一行，谁、何时、改成什么。
-- 去掉环境许可名单（DATAFABRIC_HISTORY_SOURCE_ALLOWLIST）后多开放的来源，原名单的值找不回：可读开关先关上
-- （和 fetch/catalog.py 的 DEFAULT_CLOSED 同一份名单；新库补行时由 runner 按那份名单关）。NCCN 一定关。
ALTER TABLE fabric_sources ADD COLUMN IF NOT EXISTS fetch_enabled boolean NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS fabric_source_switch_log (
  id bigserial PRIMARY KEY,
  source_id text NOT NULL REFERENCES fabric_sources(source_id),
  changed_by text NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  read_enabled boolean NOT NULL,   -- 改完以后的值
  fetch_enabled boolean NOT NULL
);
-- 管理接口取每个来源最近的一次任务
CREATE INDEX IF NOT EXISTS fabric_runs_source_created ON fabric_runs (source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS fabric_source_switch_log_source ON fabric_source_switch_log (source_id, changed_at DESC);

WITH closed AS (
  UPDATE fabric_sources SET read_enabled = false
  WHERE read_enabled AND source_id IN (
    'cde_zdyz', 'fda_drug_safety', 'fda_press', 'fiercepharma', 'gdelt_doc', 'hkex', 'local_files', 'nccn',
    'phirda_industry', 'phirda_policy', 'phirda_rd', 'phirda_healthcare_reform', 'pubmed', 'roche', 'sec_companyfacts')
  RETURNING source_id, read_enabled, fetch_enabled)
INSERT INTO fabric_source_switch_log (source_id, changed_by, read_enabled, fetch_enabled)
SELECT source_id, 'migration V047', read_enabled, fetch_enabled FROM closed;
