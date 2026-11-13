-- 索引待办按“派生产物”区分（Codex r59）：处理器升级后同一版本会有新的正文产物。
-- 每个（目标, 版本, 产物, 分片）一条待办，唯一键在 V028（带分片序号）；旧产物的待办由 index_publication 判为
-- superseded 并停用，记录保留。改造前的待办 artifact_key 为空串。
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS artifact_key text NOT NULL DEFAULT '';
ALTER TABLE index_publications DROP CONSTRAINT IF EXISTS index_publications_target_id_version_id_key;
