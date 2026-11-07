-- History library: checked measurement objects (contracts.data_objects.MeasurementV2) and their history. An object
-- is a reading checked consistent by one checker version (measurement_id = digest of reading and checker); its row
-- never changes. What happens to it later - superseded by a newer clean artifact, producer or checker, corrected,
-- retracted, a correction withdrawn - is an append-only event with three times: when the material that caused it
-- became visible (and its proof, if any), when Fabric produced the result, and when the state takes effect. The
-- state at a time is folded from the events; published_seq orders the events of one demo kind for the change feed
-- and is taken under the row lock of derived_measurement_feed, so the feed's order is the commit order.
CREATE TABLE IF NOT EXISTS derived_measurements (
 measurement_id char(64) PRIMARY KEY,
 reading_id char(64) NOT NULL REFERENCES derived_measure_readings(reading_id),
 run_id char(64) NOT NULL,
 version_id char(64) NOT NULL,
 source_id text NOT NULL,
 source_record_id text NOT NULL,
 is_demo boolean NOT NULL,
 text_processor_version text NOT NULL,
 text_sha256 char(64) NOT NULL,
 producer_identity text NOT NULL,
 checker_version text NOT NULL,
 contract_version text NOT NULL,
 check_run_id text NOT NULL,
 check_row_id bigint NOT NULL,
 body jsonb NOT NULL,
 first_seen_at timestamptz NOT NULL,
 proof_available_at timestamptz,
 produced_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (reading_id, checker_version)
);
CREATE INDEX IF NOT EXISTS derived_measurements_version ON derived_measurements (version_id, is_demo);
CREATE INDEX IF NOT EXISTS derived_measurements_run ON derived_measurements (run_id);
CREATE OR REPLACE RULE derived_measurements_no_update AS ON UPDATE TO derived_measurements DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_measurements_no_delete AS ON DELETE TO derived_measurements DO INSTEAD NOTHING;

CREATE TABLE IF NOT EXISTS derived_measurement_feed (
 is_demo boolean PRIMARY KEY,
 last_seq bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_measurement_events (
 event_id bigserial PRIMARY KEY,
 is_demo boolean NOT NULL,
 published_seq bigint NOT NULL,
 measurement_id char(64) NOT NULL REFERENCES derived_measurements(measurement_id),
 source_id text NOT NULL,
 version_id char(64) NOT NULL,
 kind text NOT NULL CHECK (kind IN ('created','superseded_by_processor','superseded_by_producer',
                                    'rechecked_by_checker','corrected','retracted','correction_withdrawn')),
 cause_key text NOT NULL,
 cause jsonb NOT NULL,
 -- the source whose permission the cause depends on (a correcting version's source); NULL when it is Fabric's own
 cause_source_id text,
 superseded_by char(64),
 material_visible_at timestamptz NOT NULL,
 material_proof_at timestamptz,
 produced_at timestamptz NOT NULL,
 effective_at timestamptz NOT NULL,
 versions jsonb NOT NULL,
 UNIQUE (is_demo, published_seq),
 UNIQUE (measurement_id, kind, cause_key)
);
CREATE INDEX IF NOT EXISTS derived_measurement_events_object ON derived_measurement_events (measurement_id, event_id);
CREATE OR REPLACE RULE derived_measurement_events_no_update AS ON UPDATE TO derived_measurement_events DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_measurement_events_no_delete AS ON DELETE TO derived_measurement_events DO INSTEAD NOTHING;

-- who a measurement's object is, only from an identity confirmed in a fixed graph package (confirmed by audited
-- review, or rule_confirmed by a native-id rule) whose evidence points at this measurement's mention: same version,
-- same clean artifact, a span holding the object literal. Without such a row the identity is a record-local literal.
CREATE TABLE IF NOT EXISTS derived_measurement_identities (
 id bigserial PRIMARY KEY,
 measurement_id char(64) NOT NULL REFERENCES derived_measurements(measurement_id),
 role text NOT NULL CHECK (role IN ('object')),
 namespace text NOT NULL,
 identity_id text NOT NULL,
 identity_status text NOT NULL CHECK (identity_status IN ('confirmed','rule_confirmed')),
 graph_source_id text NOT NULL,
 graph_record_id text NOT NULL,
 graph_version_id char(64) NOT NULL,
 graph_first_seen_at timestamptz NOT NULL,
 graph_proof_available_at timestamptz,
 evidence jsonb NOT NULL,
 produced_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (measurement_id, role, graph_version_id, namespace, identity_id)
);
CREATE OR REPLACE RULE derived_measurement_identities_no_update AS ON UPDATE TO derived_measurement_identities DO INSTEAD NOTHING;
CREATE OR REPLACE RULE derived_measurement_identities_no_delete AS ON DELETE TO derived_measurement_identities DO INSTEAD NOTHING;
