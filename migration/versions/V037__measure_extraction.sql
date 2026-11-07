-- History library: the measurement extraction of the processing layer (datafabric.measure_extract). A run is one
-- reading of one clean artifact of one fixed original by one producer (processor version + model configuration
-- digest); run_id is the digest of that key, so planning twice inserts nothing. Its windows are one lease queue;
-- every processing of a window appends an attempt (with the model's raw output), every model HTTP request is
-- registered before it is sent and settled exactly once after it, and every reading is the strict candidate it is
-- checked with. A failed window is owed (failed / failed_final) until it is explicitly retried; a spent budget is
-- not a failure. No foreign key to history_versions (test fixtures truncate it): ids come from the artifacts' rows.
CREATE TABLE IF NOT EXISTS derived_measure_runs (
 run_id char(64) PRIMARY KEY,
 version_id char(64) NOT NULL,
 source_id text NOT NULL,
 source_record_id text NOT NULL,
 is_demo boolean NOT NULL,
 raw_sha256 char(64) NOT NULL,
 text_processor_version text NOT NULL,
 text_sha256 char(64) NOT NULL,
 producer text NOT NULL,
 producer_version text NOT NULL,
 model_config_sha256 char(64),
 model_config jsonb,
 producer_identity text NOT NULL,
 window_chars integer NOT NULL,
 overlap_chars integer NOT NULL,
 windows_total integer NOT NULL,
 state text NOT NULL CHECK (state IN ('reading','complete','partial','failed','paused_unreadable')),
 reason text,
 supersedes_run_id char(64),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS derived_measure_runs_version ON derived_measure_runs (version_id, producer_identity);
CREATE INDEX IF NOT EXISTS derived_measure_runs_paused ON derived_measure_runs (source_id) WHERE state = 'paused_unreadable';

CREATE TABLE IF NOT EXISTS derived_measure_windows (
 run_id char(64) NOT NULL REFERENCES derived_measure_runs(run_id),
 window_index integer NOT NULL CHECK (window_index >= 0),
 start_char integer NOT NULL,
 end_char integer NOT NULL,
 state text NOT NULL CHECK (state IN ('pending','sent','failed','failed_final','budget_spent','source_unreadable','skipped')),
 attempts integer NOT NULL DEFAULT 0,
 next_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_token text,
 lease_until timestamptz,
 reason text,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (run_id, window_index),
 CHECK (end_char > start_char AND start_char >= 0)
);
CREATE INDEX IF NOT EXISTS derived_measure_windows_due
 ON derived_measure_windows (next_at) WHERE state IN ('pending','failed','budget_spent');

CREATE TABLE IF NOT EXISTS derived_measure_window_attempts (
 id bigserial PRIMARY KEY,
 run_id char(64) NOT NULL,
 window_index integer NOT NULL,
 outcome text NOT NULL,
 error text,
 call_id text,
 requests integer NOT NULL DEFAULT 0,
 output_sha256 char(64),
 output text,
 mappings jsonb NOT NULL DEFAULT '[]'::jsonb,
 measures_found integer,
 reading_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
 not_built jsonb NOT NULL DEFAULT '[]'::jsonb,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS derived_measure_window_attempts_window ON derived_measure_window_attempts (run_id, window_index);
CREATE OR REPLACE RULE derived_measure_window_attempts_no_update AS ON UPDATE TO derived_measure_window_attempts DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_measure_window_attempts_no_delete AS ON DELETE TO derived_measure_window_attempts DO INSTEAD NOTHING;

-- one row per model HTTP request: registered (before it is sent) in its own committed transaction, settled once
-- (after its outcome) with the raw response; a registration without an outcome is an unknown outcome, never zero
CREATE TABLE IF NOT EXISTS derived_measure_requests (
 call_id text NOT NULL,
 request_index integer NOT NULL CHECK (request_index >= 1),
 run_id char(64) NOT NULL,
 window_index integer NOT NULL,
 body_sha256 char(64) NOT NULL,
 endpoint_id text NOT NULL,
 model text NOT NULL,
 registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 delivered text CHECK (delivered IN ('yes','no','unknown')),
 http_status integer,
 error text,
 exception text,
 timed_out boolean,
 response_sha256 char(64),
 response_text text,
 seconds double precision,
 settled_at timestamptz,
 PRIMARY KEY (call_id, request_index)
);
CREATE INDEX IF NOT EXISTS derived_measure_requests_window ON derived_measure_requests (run_id, window_index);
CREATE OR REPLACE FUNCTION derived_measure_requests_settle_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN
  RAISE EXCEPTION 'derived_measure_requests is never deleted';
 END IF;
 IF OLD.settled_at IS NOT NULL OR NEW.settled_at IS NULL
    OR (NEW.call_id, NEW.request_index, NEW.run_id, NEW.window_index, NEW.body_sha256, NEW.endpoint_id, NEW.model,
        NEW.registered_at)
       IS DISTINCT FROM (OLD.call_id, OLD.request_index, OLD.run_id, OLD.window_index, OLD.body_sha256,
        OLD.endpoint_id, OLD.model, OLD.registered_at) THEN
  RAISE EXCEPTION 'a model request is registered once and settled once';
 END IF;
 RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS derived_measure_requests_settle_once ON derived_measure_requests;
CREATE TRIGGER derived_measure_requests_settle_once BEFORE UPDATE OR DELETE ON derived_measure_requests
 FOR EACH ROW EXECUTE FUNCTION derived_measure_requests_settle_once();

CREATE TABLE IF NOT EXISTS derived_measure_readings (
 reading_id char(64) PRIMARY KEY,
 run_id char(64) NOT NULL REFERENCES derived_measure_runs(run_id),
 window_index integer NOT NULL,
 version_id char(64) NOT NULL,
 source_id text NOT NULL,
 is_demo boolean NOT NULL,
 text_processor_version text NOT NULL,
 text_sha256 char(64) NOT NULL,
 producer_identity text NOT NULL,
 quote_start integer NOT NULL,
 quote_end integer NOT NULL,
 item jsonb NOT NULL,
 passages jsonb NOT NULL,
 final jsonb NOT NULL,
 measure jsonb NOT NULL,
 relocated_from char(64),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS derived_measure_readings_task
 ON derived_measure_readings (version_id, producer_identity, text_processor_version);
CREATE INDEX IF NOT EXISTS derived_measure_readings_run ON derived_measure_readings (run_id);
CREATE OR REPLACE RULE derived_measure_readings_no_update AS ON UPDATE TO derived_measure_readings DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_measure_readings_no_delete AS ON DELETE TO derived_measure_readings DO INSTEAD NOTHING;

-- after a clean-text upgrade: where each reading of the superseded run went in the new run - read again, relocated
-- (its quote once in the new text: a reading checked without a model call), owed (quote several times, or its
-- literals no longer located) or gone (quote not in the new text). Written once per (old reading, new run).
CREATE TABLE IF NOT EXISTS derived_measure_relocations (
 old_reading_id char(64) NOT NULL,
 new_run_id char(64) NOT NULL,
 outcome text NOT NULL CHECK (outcome IN ('reread','relocated','owed','gone')),
 reason text,
 new_reading_id char(64),
 quote_occurrences integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (old_reading_id, new_run_id)
);
CREATE OR REPLACE RULE derived_measure_relocations_no_update AS ON UPDATE TO derived_measure_relocations DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_measure_relocations_no_delete AS ON DELETE TO derived_measure_relocations DO INSTEAD NOTHING;
