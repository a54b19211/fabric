-- 独立历史资料表；在DATAFABRIC_HISTORY_DATABASE_URL执行，不改旧documents/图谱表。
CREATE TABLE IF NOT EXISTS history_sources (
 source_id text PRIMARY KEY,
 catalog jsonb NOT NULL,
 read_enabled boolean NOT NULL DEFAULT true,
 published_seq bigint NOT NULL DEFAULT 0 CHECK (published_seq >= 0)
);
CREATE TABLE IF NOT EXISTS history_runs (
 id uuid PRIMARY KEY,
 source_id text NOT NULL REFERENCES history_sources(source_id),
 is_demo boolean NOT NULL,
 kind text NOT NULL CHECK (kind IN ('archive','fetch')),
 state text NOT NULL CHECK (state IN ('queued','running','completed','partial','failed','enqueue_failed')),
 attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
 request_hash char(64) NOT NULL,
 idempotency_key text NOT NULL UNIQUE,
 request_payload jsonb NOT NULL,
 scope_description text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 completed_at timestamptz,
 job_id text,
 version_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
 manifest_sha256 char(64),
 coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
 error jsonb,
 attempts jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE TABLE IF NOT EXISTS history_versions (
 version_id char(64) PRIMARY KEY,
 source_id text NOT NULL REFERENCES history_sources(source_id),
 is_demo boolean NOT NULL,
 source_record_id text NOT NULL,
 record_kind text NOT NULL CHECK (record_kind IN ('trial','program','outcome','document')),
 first_seen_at timestamptz NOT NULL,
 received_at timestamptz NOT NULL,
 proof_available_at timestamptz,
 published_seq bigint NOT NULL CHECK (published_seq > 0),
 payload jsonb NOT NULL,
 UNIQUE (source_id,published_seq)
);
CREATE INDEX IF NOT EXISTS history_versions_selection ON history_versions
 (source_id,is_demo,source_record_id,published_seq DESC);
CREATE INDEX IF NOT EXISTS history_versions_replay ON history_versions
 (source_id,is_demo,proof_available_at,published_seq);
-- 版本没有更新/删除业务入口；规则额外阻止误写，已有采用记录保留。
CREATE OR REPLACE RULE history_versions_no_update AS ON UPDATE TO history_versions DO INSTEAD NOTHING;
CREATE OR REPLACE RULE history_versions_no_delete AS ON DELETE TO history_versions DO INSTEAD NOTHING;
