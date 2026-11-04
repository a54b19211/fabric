-- 索引待办按“派生产物”区分（Codex r59）：附件正文来自加工层，处理器升级后同一附件版本会有新的正文产物。
-- 每个（目标, 版本, 产物）一条待办；旧产物的待办由 index_publication 判为 superseded 并停用，记录保留。
-- 非附件版本 artifact_key 为空串，行为与原来相同。
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS artifact_key text NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS index_publications_artifact ON index_publications (target_id, version_id, artifact_key);
ALTER TABLE index_publications DROP CONSTRAINT IF EXISTS index_publications_target_id_version_id_key;
