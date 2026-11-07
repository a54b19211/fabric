-- History library: corrections and retractions a source states for a record without naming the target version.
-- They cannot become links (derived_measure_correction_links needs a version), so they are kept as claims on the
-- target record: while a claim is asserted and not resolved, every measurement object of that record is "unknown"
-- (correction_target_version_unknown), never silently ok. A resolution names the version (who, on what basis);
-- from then the normal link and its corrected / retracted events apply. A later version of the correcting record
-- that no longer states it withdraws the claim. Claims are written by the same bounded scan as the links; an
-- explicit re-scan (cursor set back) records nothing twice. Append-only, structure only (no rows).
CREATE TABLE IF NOT EXISTS derived_measure_correction_claims (
 id bigserial PRIMARY KEY,
 event_version_id char(64) NOT NULL,
 target_source_id text NOT NULL,
 target_record_id text NOT NULL,
 relation text NOT NULL CHECK (relation IN ('corrects','retracts')),
 kind text NOT NULL CHECK (kind IN ('asserted','withdrawn')),
 origin_version_id char(64) NOT NULL,
 source_id text NOT NULL,
 source_record_id text NOT NULL,
 is_demo boolean NOT NULL,
 first_seen_at timestamptz NOT NULL,
 proof_available_at timestamptz,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (event_version_id, target_source_id, target_record_id, relation, kind, origin_version_id)
);
CREATE INDEX IF NOT EXISTS derived_measure_correction_claims_target
 ON derived_measure_correction_claims (target_source_id, target_record_id, is_demo);
CREATE INDEX IF NOT EXISTS derived_measure_correction_claims_record
 ON derived_measure_correction_claims (source_id, source_record_id, is_demo);
DROP TRIGGER IF EXISTS derived_measure_correction_claims_append_only ON derived_measure_correction_claims;
CREATE TRIGGER derived_measure_correction_claims_append_only BEFORE UPDATE OR DELETE ON derived_measure_correction_claims
 FOR EACH ROW EXECUTE FUNCTION derived_measure_append_only();

CREATE TABLE IF NOT EXISTS derived_measure_correction_resolutions (
 claim_id bigint PRIMARY KEY REFERENCES derived_measure_correction_claims(id),
 target_version_id char(64) NOT NULL,
 actor text NOT NULL,
 basis text NOT NULL,
 resolved_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DROP TRIGGER IF EXISTS derived_measure_correction_resolutions_append_only ON derived_measure_correction_resolutions;
CREATE TRIGGER derived_measure_correction_resolutions_append_only BEFORE UPDATE OR DELETE
 ON derived_measure_correction_resolutions FOR EACH ROW EXECUTE FUNCTION derived_measure_append_only();
