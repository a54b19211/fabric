-- T-30 index scheduling: per class and source claim cursor (round robin inside a job class), a shared send
-- bucket per knowledge base, gate counters (denials and blocked time), and the size of each sent upload so
-- the in-flight budget counts characters and bytes as well as entries.
CREATE TABLE IF NOT EXISTS index_claim_cursor (
  class text NOT NULL,
  source_id text NOT NULL,
  last_claimed_at timestamptz NOT NULL,
  PRIMARY KEY (class, source_id)
);
CREATE TABLE IF NOT EXISTS index_send_bucket (
  knowledge_base_id text PRIMARY KEY,
  tokens double precision NOT NULL,
  refilled_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS index_gate_stats (
  knowledge_base_id text NOT NULL,
  reason text NOT NULL,
  denials bigint NOT NULL DEFAULT 0,
  blocked_seconds double precision NOT NULL DEFAULT 0,
  last_at timestamptz,
  PRIMARY KEY (knowledge_base_id, reason)
);
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS sent_chars bigint;
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS sent_bytes bigint;
CREATE INDEX IF NOT EXISTS index_publications_open_by_kb
  ON index_publications ((manifest->>'knowledge_base_id'))
  WHERE state NOT IN ('ready', 'disabled');
