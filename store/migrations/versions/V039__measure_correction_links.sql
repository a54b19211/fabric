-- History library: the corrections and retractions sources state (document evidence `document_relations` with a
-- target version), as links the measurement objects of the target version follow. A link is found by a bounded
-- scan of each source's versions in published order (a cursor per source), never by scanning version
-- payloads at read time. A link is asserted by the version that states it; a later version of the same correcting
-- record that no longer states it withdraws it (a row of its own: nothing is updated or deleted).
CREATE TABLE IF NOT EXISTS derived_measure_correction_links (
 event_version_id char(64) NOT NULL,
 target_version_id char(64) NOT NULL,
 relation text NOT NULL CHECK (relation IN ('corrects','retracts')),
 kind text NOT NULL CHECK (kind IN ('asserted','withdrawn')),
 -- for a withdrawal: the version whose assertion it withdraws; for an assertion: the asserting version itself
 origin_version_id char(64) NOT NULL,
 source_id text NOT NULL,
 source_record_id text NOT NULL,
 is_demo boolean NOT NULL,
 first_seen_at timestamptz NOT NULL,
 proof_available_at timestamptz,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (event_version_id, target_version_id, relation, kind, origin_version_id)
);
CREATE INDEX IF NOT EXISTS derived_measure_correction_links_target ON derived_measure_correction_links (target_version_id);
CREATE INDEX IF NOT EXISTS derived_measure_correction_links_record
 ON derived_measure_correction_links (source_id, source_record_id, is_demo);
DROP TRIGGER IF EXISTS derived_measure_correction_links_append_only ON derived_measure_correction_links;
CREATE TRIGGER derived_measure_correction_links_append_only BEFORE UPDATE OR DELETE ON derived_measure_correction_links
 FOR EACH ROW EXECUTE FUNCTION derived_measure_append_only();

-- published_seq is per source (demo and real versions share it), so is the cursor
CREATE TABLE IF NOT EXISTS derived_measure_correction_cursor (
 source_id text PRIMARY KEY,
 last_seq bigint NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
