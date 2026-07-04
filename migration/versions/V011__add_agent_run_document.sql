-- V011 — agent_run 表增加 document_uuid 和 batch_id 列，支持按文档维度聚合审核
BEGIN;

ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS document_uuid VARCHAR(64) DEFAULT '';
ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS batch_id VARCHAR(64) DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_agent_run_document_uuid ON agent_run(document_uuid);
CREATE INDEX IF NOT EXISTS idx_agent_run_batch_id ON agent_run(batch_id);

COMMIT;
