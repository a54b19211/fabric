-- V014: entity_merge_log 表
-- 记录每次实体合并操作的完整快照，支持追溯和回滚。
-- 对应 db/models.py EntityMergeLog 模型。

CREATE TABLE IF NOT EXISTS entity_merge_log (
    id                  BIGSERIAL PRIMARY KEY,
    keeper_dict_id      BIGINT NOT NULL,
    absorbed_dict_ids   BIGINT[] NOT NULL,
    before_snapshot     JSONB NOT NULL,
    after_snapshot      JSONB NOT NULL,
    field_decisions     JSONB DEFAULT '{}',
    merge_type          TEXT DEFAULT 'identity',
    reviewer            TEXT DEFAULT '',
    note                TEXT DEFAULT '',
    is_rolled_back      BOOLEAN DEFAULT FALSE,
    rolled_back_at      TIMESTAMPTZ,
    rollback_reason     TEXT DEFAULT '',
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eml_keeper_dict ON entity_merge_log(keeper_dict_id);
CREATE INDEX IF NOT EXISTS idx_eml_created_at ON entity_merge_log(created_at DESC);
