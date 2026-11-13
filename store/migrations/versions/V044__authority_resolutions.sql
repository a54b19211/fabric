-- 徐子雯的编号对齐结果（clean/align.py，由 clean/quality_stage.py 写）：一个版本、一个对齐版本下，每个药名、每个编号字段
-- 一行，只追加。列照她对象库的 doc_authority_resolution，改按 version_id 存（固定版本永远对得上同一份原件）；
-- 她的 source_id（pubchem / chembl / format…）这里叫 authority，免得和数据来源 source_id 混。
-- 药名行的 sources 存每个候选名、每个权威库各自的结果，离线可以换取舍策略重算，不用再联网。
CREATE TABLE IF NOT EXISTS derived_authority_resolutions (
  version_id char(64) NOT NULL,           -- history_versions 的版本号（版本只追加，不设外键，和测量表一样）
  aligner_version text NOT NULL,
  id_type text NOT NULL,           -- drug_name / nct_id / pmid / doi / application_number / cas_number
  resolution_key text NOT NULL,    -- 药名：原始药名；编号：字段名（如 nct_id）
  raw_value text,                  -- 归一前的原值
  candidate text,                  -- 实际命中的候选名（药名归一后）
  rule text,                       -- 命中的归一规则（as_is / cut_descriptor / inner_alias …）
  candidates_tried integer,        -- 试了几个候选名（请求成本）
  found boolean NOT NULL,
  degraded boolean NOT NULL,       -- 没查成（超时、限流、不出网）：不算“查了没有”
  skipped boolean NOT NULL,        -- 占位名（安慰剂、盐水）：不进命中率分母
  std_id text,                     -- PUBCHEM:2244 / CHEMBL:CHEMBL25 / NCT:01234567 …
  authority text,                  -- 哪个库给的：pubchem / chembl / ols4 / uniprot / gsrs / local / format / none
  resolved_value text,             -- 库里的规范名
  confidence double precision,
  strategy text,                   -- 多来源取舍用的策略
  sources jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (version_id, aligner_version, id_type, resolution_key)
);
CREATE INDEX IF NOT EXISTS derived_authority_resolutions_std_id ON derived_authority_resolutions (std_id) WHERE found;
CREATE OR REPLACE RULE derived_authority_resolutions_no_update AS ON UPDATE TO derived_authority_resolutions
  DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_authority_resolutions_no_delete AS ON DELETE TO derived_authority_resolutions
  DO INSTEAD NOTHING;
