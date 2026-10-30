-- 固定版本索引是可重建投影；与源端outbox分开，索引故障不阻塞原件提交。
CREATE TABLE IF NOT EXISTS index_targets (
 id char(64) PRIMARY KEY,
 config jsonb NOT NULL,
 enabled boolean NOT NULL DEFAULT true,
 scanned_seq bigint NOT NULL DEFAULT 0,
 scanned_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS index_publications (
 id char(64) PRIMARY KEY,
 target_id char(64) NOT NULL REFERENCES index_targets(id),
 version_id char(64) NOT NULL REFERENCES history_versions(version_id),
 source_record_id text NOT NULL,
 remote_id char(64) NOT NULL,
 manifest jsonb NOT NULL,
 state text NOT NULL DEFAULT 'queued',
 desired_state text NOT NULL DEFAULT 'active',
 first_send_at timestamptz,
 knowledge_id text,
 receipt jsonb NOT NULL DEFAULT '{}'::jsonb,
 error jsonb,
 attempts integer NOT NULL DEFAULT 0,
 lease_token text,
 lease_until timestamptz,
 next_check_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(target_id, version_id)
);
CREATE INDEX IF NOT EXISTS index_publications_due ON index_publications(next_check_at, id);
CREATE INDEX IF NOT EXISTS index_publications_record ON index_publications(target_id, source_record_id);
