-- V013: 身份合并提案表
-- Agent D (entity_merge) 判断 extracted_entity 应链接到 entity_dict 时，
-- 写入此表提交人工审核，不再复用 entity_merge_queue。

CREATE TABLE IF NOT EXISTS entity_identity_merge_proposal (
    id                  BIGSERIAL PRIMARY KEY,
    source_entity_id    BIGINT NOT NULL,
    target_dict_id      BIGINT NOT NULL,
    source_entity_snapshot JSONB NOT NULL DEFAULT '{}',
    target_dict_snapshot   JSONB NOT NULL DEFAULT '{}',
    agent_run_id        TEXT DEFAULT '',
    agent_reason        TEXT DEFAULT '',
    agent_confidence    DOUBLE PRECISION DEFAULT 0.0,
    status              TEXT DEFAULT 'pending',  -- pending | approved | rejected
    reviewer            TEXT DEFAULT '',
    review_note         TEXT DEFAULT '',
    reviewed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eimp_status ON entity_identity_merge_proposal(status);
CREATE INDEX IF NOT EXISTS idx_eimp_source_entity ON entity_identity_merge_proposal(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_eimp_target_dict ON entity_identity_merge_proposal(target_dict_id);
