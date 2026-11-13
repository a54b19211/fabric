-- v3 6.2 (profile v5): per-version experiment type candidates and obtainable reading level, each with its
-- basis (processing.profile). Rows of earlier profile versions keep them empty.
ALTER TABLE derived_profiles ADD COLUMN IF NOT EXISTS experiment_types jsonb;
ALTER TABLE derived_profiles ADD COLUMN IF NOT EXISTS reading_level jsonb;
