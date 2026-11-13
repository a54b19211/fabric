-- 徐子雯的五维质量分（clean/quality.py，由 clean/quality_stage.py 写）：一个版本、一个评分器版本一行，只追加。
-- 评分口径一改就升 scorer_version 写新行，旧行保留（论文按版本引用）。level 只是等级：“隔离”不拦截任何东西。
CREATE TABLE IF NOT EXISTS derived_quality_scores (
  version_id char(64) NOT NULL,           -- history_versions 的版本号（版本只追加，不设外键，和测量表一样）
  scorer_version text NOT NULL,
  text_processor_version text NOT NULL,  -- 打分读的是哪一版清洗正文（derived_clean_text）
  source_id text NOT NULL,
  is_demo boolean NOT NULL,
  score double precision NOT NULL,       -- 加权总分 0~1
  level text NOT NULL CHECK (level IN ('高', '中', '低', '隔离')),
  dimensions jsonb NOT NULL,             -- 五维各自的分
  issues jsonb NOT NULL,                 -- 问题清单
  violations jsonb NOT NULL,             -- 结构校验（必填、类型、枚举）的违规，只计数不拦截
  confidence jsonb NOT NULL,             -- 来源权威分 {level, score, reason}
  fields jsonb NOT NULL,                 -- 字段清洗和 L3 规范化之后的字段（她自己的产物，不进清洗正文）
  lineage jsonb NOT NULL,                -- 字段血缘：每个字段每一步 {step, op, input, output}
  scored_at timestamptz NOT NULL,        -- 打分时刻；时效性按这一天算
  PRIMARY KEY (version_id, scorer_version)
);
CREATE INDEX IF NOT EXISTS derived_quality_scores_level ON derived_quality_scores (source_id, is_demo, scorer_version, level);
CREATE OR REPLACE RULE derived_quality_scores_no_update AS ON UPDATE TO derived_quality_scores DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_quality_scores_no_delete AS ON DELETE TO derived_quality_scores DO INSTEAD NOTHING;
