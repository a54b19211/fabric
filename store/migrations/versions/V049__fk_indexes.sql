-- Indexes on the referencing columns of the foreign keys to fabric_versions that had none. Without them every
-- version delete (dedupe) or key update scans index_publications / derived_registry_diff_runs /
-- derived_registry_changes / fabric_attachment_links in full.
CREATE INDEX IF NOT EXISTS index_publications_version_id_idx ON index_publications (version_id);
CREATE INDEX IF NOT EXISTS derived_registry_diff_runs_from_version_id_idx ON derived_registry_diff_runs (from_version_id);
CREATE INDEX IF NOT EXISTS derived_registry_changes_from_version_id_idx ON derived_registry_changes (from_version_id);
CREATE INDEX IF NOT EXISTS fabric_attachment_links_parent_version_id_idx ON fabric_attachment_links (parent_version_id);
