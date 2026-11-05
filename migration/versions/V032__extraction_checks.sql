-- v3 6.2 (T-29 P2): extraction consistency of processing products, checked by BioCheck. A task is registered in
-- the same transaction as the products it checks (identifiers, profile annotations) and processed outside it
-- by the check worker. Results are append-only and versioned: contract, checker, producer, the fixed artifact
-- and the full canonical request digest. No foreign key to history_versions (test fixtures truncate it), the
-- version ids come from the products' own rows.
CREATE TABLE IF NOT EXISTS derived_extraction_check_tasks (
  version_id char(64) NOT NULL,
  producer text NOT NULL,
  producer_version text NOT NULL,
  text_processor_version text NOT NULL,
  state text NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0,
  next_check_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  lease_token text,
  lease_until timestamptz,
  error jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (version_id, producer, producer_version, text_processor_version)
);
CREATE INDEX IF NOT EXISTS derived_extraction_check_tasks_due
  ON derived_extraction_check_tasks (next_check_at) WHERE state IN ('queued', 'waiting');
CREATE TABLE IF NOT EXISTS derived_extraction_checks (
  id bigserial PRIMARY KEY,
  version_id char(64) NOT NULL,
  source_id text NOT NULL,
  is_demo boolean NOT NULL,
  producer text NOT NULL,
  producer_version text NOT NULL,
  text_processor_version text NOT NULL,
  text_sha256 text NOT NULL,
  contract_version text NOT NULL,
  checker_version text,
  request_sha256 char(64) NOT NULL,
  status text NOT NULL,
  error_code text,
  items jsonb NOT NULL DEFAULT '[]'::jsonb,
  counts jsonb,
  calls jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS derived_extraction_checks_version ON derived_extraction_checks (version_id, producer);
CREATE INDEX IF NOT EXISTS derived_extraction_checks_reuse
  ON derived_extraction_checks (request_sha256, checker_version, is_demo) WHERE status = 'completed';
CREATE OR REPLACE RULE derived_extraction_checks_no_update AS ON UPDATE TO derived_extraction_checks DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_extraction_checks_no_delete AS ON DELETE TO derived_extraction_checks DO INSTEAD NOTHING;
