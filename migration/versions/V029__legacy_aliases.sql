-- v3 6.3: what each legacy version became (collection.legacy_migration). One row per legacy version:
-- imported (the real source, record and migrated version, a native version with the same content only as a
-- relation) or pending (with the reason it could not be proven). Legacy versions and their audit stay as they
-- are: this table only maps them.
CREATE TABLE IF NOT EXISTS legacy_aliases (
  legacy_version_id character(64) PRIMARY KEY,
  legacy_record_id text NOT NULL,
  is_demo boolean NOT NULL,
  source_site text,
  status text NOT NULL,
  reason text,
  basis text,
  real_source_id text,
  real_record_id text,
  real_version_id character(64),
  equivalent_version_id character(64),
  claimed jsonb NOT NULL DEFAULT '{}'::jsonb,
  converter text NOT NULL,
  migrated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT legacy_aliases_status CHECK (status IN ('imported', 'pending')),
  CONSTRAINT legacy_aliases_target CHECK ((status = 'imported') = (real_version_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS legacy_aliases_real ON legacy_aliases (real_source_id, real_record_id);
