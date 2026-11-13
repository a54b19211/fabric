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
ALTER TABLE index_publications ADD COLUMN IF NOT EXISTS attempt_floor integer;
ALTER TABLE index_gate_stats ADD COLUMN IF NOT EXISTS closed_since timestamptz;
-- sizes of uploads sent before this migration: a piece knows its own size, a whole entry is its clean text.
-- A row still without a size is unknown: it blocks new uploads of its knowledge base until its size is known
-- or it settles (the current single-upload limit proves nothing about an old upload).
UPDATE index_publications SET sent_chars=(piece->>'chars')::bigint,sent_bytes=(piece->>'bytes')::bigint
  WHERE sent_chars IS NULL AND piece IS NOT NULL AND piece ? 'chars' AND piece ? 'bytes';
UPDATE index_publications p SET sent_chars=length(c.text),sent_bytes=octet_length(c.text)
  FROM derived_clean_text c
  WHERE p.sent_chars IS NULL AND p.piece IS NULL AND c.version_id=p.version_id AND c.status='parsed'
    AND c.processor_version||':'||c.text_sha256=p.artifact_key;
