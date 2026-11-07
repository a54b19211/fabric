-- History library: how a measurement run was selected (task book 01, 2.7). NULL: planned by the standing queue (every
-- current clean artifact of a measurable source, the versions of a bounded backfill left out). Otherwise the explicit
-- list that named the version: {"scope_sha256": digest of the list, "windows": [indexes] or null for all windows}.
-- A window the list did not name is stored 'skipped' with reason 'out_of_scope', so it is never owed and never
-- deleted afterwards. Structure only (no rows).
ALTER TABLE derived_measure_runs ADD COLUMN IF NOT EXISTS selection jsonb;
