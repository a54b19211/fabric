-- V009 — Agent 自动审核运行记录表
BEGIN;

CREATE TABLE IF NOT EXISTS agent_run (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(64) UNIQUE NOT NULL,
    agent_type VARCHAR(32) NOT NULL,     -- entity_review | relation_review | conflict_resolution | semantic_match
    target_id INTEGER,                    -- entity_id / relation_id / merge_id / dict_id
    status VARCHAR(16) DEFAULT 'completed',  -- queued | running | completed | failed | escalated
    decision VARCHAR(32) DEFAULT '',      -- approved | rejected | keep_existing | accept_new | custom | escalated | error
    reason TEXT DEFAULT '',
    confidence FLOAT DEFAULT 0.0,
    steps JSONB DEFAULT '[]',             -- [{step: N, type: "ai"|"tool", tool_name?: "", content: ""}]
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_run_type_target ON agent_run(agent_type, target_id);
CREATE INDEX IF NOT EXISTS idx_agent_run_status ON agent_run(status);
CREATE INDEX IF NOT EXISTS idx_agent_run_created ON agent_run(created_at DESC);

COMMIT;
