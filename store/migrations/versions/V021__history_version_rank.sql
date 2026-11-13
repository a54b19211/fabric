-- The two ranking keys of a version, derived from its payload once (live 2026-11-03: listing Europe PMC
-- read every payload of its 23927 versions, about 2s per pass, and timed out at 5s). A separate table,
-- because history_versions takes no UPDATE. A version without a row here is ranked from its payload, with
-- the same values, so a partial backfill changes no result. Old binaries never read this table.
CREATE TABLE IF NOT EXISTS history_version_rank (
  version_id text PRIMARY KEY,
  publication_role text NOT NULL,
  source_updated_rank timestamptz
);
CREATE OR REPLACE RULE history_version_rank_no_update AS ON UPDATE TO history_version_rank DO INSTEAD NOTHING;
CREATE OR REPLACE RULE history_version_rank_no_delete AS ON DELETE TO history_version_rank DO INSTEAD NOTHING;
