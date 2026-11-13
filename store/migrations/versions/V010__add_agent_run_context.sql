-- V010 — agent_run 表增加实体名称和文档标题上下文列
BEGIN;

ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS entity_name VARCHAR(512) DEFAULT '';
ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS document_title VARCHAR(512) DEFAULT '';

COMMIT;
