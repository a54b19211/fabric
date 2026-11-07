-- History library: the lifecycle of a source, as an append-only log of events (never updated, never deleted).
-- A retirement has two dates that are different facts: the DECISION (what was decided and on which day) and the time it
-- TOOK EFFECT in this deployment. The effective time is written when the deployment executes (clock_timestamp()), it is
-- never back-filled from the decision date and the decision date is never used as the effective time.
CREATE TABLE IF NOT EXISTS history_source_lifecycle (
 event_id bigserial PRIMARY KEY,
 source_id text NOT NULL,
 event text NOT NULL CHECK (event IN ('retirement_decided','retirement_effective','retirement_withdrawn')),
 at timestamptz NOT NULL,
 -- 'day': only the date is known (a decision); 'instant': the moment is known (an effect)
 at_precision text NOT NULL CHECK (at_precision IN ('day','instant')),
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 decision_ref text NOT NULL,
 actor text NOT NULL
);
CREATE INDEX IF NOT EXISTS history_source_lifecycle_source ON history_source_lifecycle(source_id, event_id);

CREATE OR REPLACE FUNCTION history_source_lifecycle_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'history_source_lifecycle is append-only';
END
$$;
DROP TRIGGER IF EXISTS history_source_lifecycle_append_only ON history_source_lifecycle;
CREATE TRIGGER history_source_lifecycle_append_only BEFORE UPDATE OR DELETE ON history_source_lifecycle
 FOR EACH ROW EXECUTE FUNCTION history_source_lifecycle_append_only();
DROP TRIGGER IF EXISTS history_source_lifecycle_no_truncate ON history_source_lifecycle;
CREATE TRIGGER history_source_lifecycle_no_truncate BEFORE TRUNCATE ON history_source_lifecycle
 FOR EACH STATEMENT EXECUTE FUNCTION history_source_lifecycle_append_only();

-- The one decision on record: legacy_documents is retired (host ruling of 2026-11-06, design record v3.1 6.3). The date is
-- what is known; its time of day is not, so it is stored as the start of that day in the host's time zone with
-- precision 'day'. The effective event is written by the deployment (python -m migration.source_lifecycle effective).
INSERT INTO history_source_lifecycle(source_id, event, at, at_precision, decision_ref, actor)
SELECT 'legacy_documents', 'retirement_decided', TIMESTAMPTZ '2026-11-06 00:00:00+08', 'day',
       '雷达全链路设计-v3.1.md#6.3', 'migration:V036'
WHERE NOT EXISTS (SELECT 1 FROM history_source_lifecycle WHERE source_id = 'legacy_documents' AND event = 'retirement_decided');
