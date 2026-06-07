-- V006 — 操作审计日志表
-- 记录所有数据库写操作的审计追踪，区分进程源（backend / crawler / worker）

CREATE TABLE IF NOT EXISTS operation_log (
    id              BIGSERIAL    PRIMARY KEY,
    operation       TEXT         NOT NULL DEFAULT '',   -- insert / update / delete / select
    target_table    TEXT         NOT NULL DEFAULT '',   -- 表名
    target_id       BIGINT       DEFAULT NULL,          -- 受影响行 id
    business_action TEXT         DEFAULT '',            -- merge / dedup / approve / reject / alias_sync / extract
    description     TEXT         DEFAULT '',            -- 一句话描述
    details         JSONB        DEFAULT '{}',          -- before/after 快照、变更字段
    operator        TEXT         DEFAULT 'system',      -- system / reviewer / disambiguation
    process         TEXT         DEFAULT '',            -- 进程源: backend / crawler / worker
    trigger_source  TEXT         DEFAULT '',            -- 触发源函数名或 API path
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_oplog_table      ON operation_log(target_table);
CREATE INDEX IF NOT EXISTS idx_oplog_action     ON operation_log(business_action);
CREATE INDEX IF NOT EXISTS idx_oplog_process    ON operation_log(process);
CREATE INDEX IF NOT EXISTS idx_oplog_created_at ON operation_log(created_at);
