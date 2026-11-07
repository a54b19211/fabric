"""Which migration files each database role runs, in order. Code, not configuration: tests constrain it.

A role is a use of a database, not a physical database: the object library (app), the History library (history) and
the original collection library the publication outbox lives in (publication). A new V file is never picked up by
a role on its own - it is added to exactly one role below, and a test fails until it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

VERSIONS_DIR = Path(__file__).parent / "versions"

# The files the object library has always run (V001-V035 of 7337e54): what installing the object library does
# today, kept as it is. Nothing after V035 is added here; a later file that the object library needs is declared in
# APP_FILES_AFTER_COMPAT with the reason.
APP_COMPAT_FILES = (
    "V001__init_schema.sql",
    "V002__add_missing_columns.sql",
    "V003__alter_embedding_dim.sql",
    "V004__add_merge_queue.sql",
    "V005__add_weknora_graph_status.sql",
    "V006__add_operation_log.sql",
    "V007__add_merge_queue_missing_columns.sql",
    "V008__add_alias_columns.sql",
    "V009__add_agent_run.sql",
    "V010__add_agent_run_context.sql",
    "V011__add_agent_run_document.sql",
    "V012__add_document_pending_entity_count.sql",
    "V013__add_identity_merge_proposal.sql",
    "V014__add_entity_merge_log.sql",
    "V015__add_name_embedding_column.sql",
    "V016__add_lineage.sql",
    "V017__history_records.sql",
    "V018__graph_record_kind.sql",
    "V019__publication_outbox.sql",
    "V020__weknora_index.sql",
    "V021__history_version_rank.sql",
    "V022__registry_changes.sql",
    "V023__derived_clean_text.sql",
    "V024__index_artifact.sql",
    "V025__clean_stage.sql",
    "V026__history_extraction_subjects.sql",
    "V027__index_publications_recent.sql",
    "V028__index_worker.sql",
    "V029__legacy_aliases.sql",
    "V030__profile_annotations.sql",
    "V031__index_scheduling.sql",
    "V032__extraction_checks.sql",
    "V033__doc_authority_resolution.sql",
    "V034__doc_authority_skipped.sql",
    "V035__identity_evidence.sql",
)
# Files after V035 that the object library also runs; none so far.
APP_FILES_AFTER_COMPAT: tuple[str, ...] = ()

# The History library: what the two former runners (migration.history, migration.index_publication) installed, in
# version order, followed by the files added after V035 for it.
HISTORY_COMPAT_FILES = (
    "V017__history_records.sql",
    "V018__graph_record_kind.sql",
    "V020__weknora_index.sql",
    "V021__history_version_rank.sql",
    "V022__registry_changes.sql",
    "V023__derived_clean_text.sql",
    "V024__index_artifact.sql",
    "V025__clean_stage.sql",
    "V027__index_publications_recent.sql",
    "V028__index_worker.sql",
    "V029__legacy_aliases.sql",
    "V030__profile_annotations.sql",
    "V031__index_scheduling.sql",
    "V032__extraction_checks.sql",
)
HISTORY_FILES_AFTER_COMPAT: tuple[str, ...] = (
    "V036__history_source_lifecycle.sql",
    # the measurement extraction of the processing layer and its checked objects (structure only, no rows)
    "V037__measure_extraction.sql",
    "V038__measurement_objects.sql",
    # the corrections sources state, as links measurement objects follow (structure only, no rows)
    "V039__measure_correction_links.sql",
    # corrections that name a record but no version, kept as claims until resolved (structure only, no rows)
    "V040__measure_correction_claims.sql",
)

# The original collection library: the publication outbox (V019) and the history subject columns on documents (V026).
PUBLICATION_FILES = ("V019__publication_outbox.sql", "V026__history_extraction_subjects.sql")
# What a throwaway library needs before the role's files can run at all (documents is an object-library table).
PUBLICATION_BASE_FILES = tuple(name for name in APP_COMPAT_FILES[:16])

# Files of the object library whose text carries its own BEGIN/COMMIT: they ran like that before the common
# runner and keep doing so. No other file may.
TRANSACTION_CONTROL_ALLOWED = frozenset({
    "V008__add_alias_columns.sql", "V009__add_agent_run.sql", "V010__add_agent_run_context.sql",
    "V011__add_agent_run_document.sql",
})


@dataclass(frozen=True)
class Manifest:
    role: str
    files: tuple[str, ...]
    lock_timeout: str
    statement_timeout: str | None
    # a table that only this role creates: if it exists while the ledger is empty, the database was migrated before
    # the ledger existed and must be taken over with --adopt-baseline, not run again
    sentinel_table: str | None = None
    # applied (but not part of the prefix) before the role's files when a throwaway library is built to
    # fingerprint them
    base_files: tuple[str, ...] = ()


MANIFESTS = {
    "app": Manifest("app", APP_COMPAT_FILES + APP_FILES_AFTER_COMPAT, lock_timeout="3s", statement_timeout=None),
    "history": Manifest("history", HISTORY_COMPAT_FILES + HISTORY_FILES_AFTER_COMPAT, lock_timeout="3s",
                        statement_timeout="60s", sentinel_table="history_sources"),
    "publication": Manifest("publication", PUBLICATION_FILES, lock_timeout="3s", statement_timeout="30s",
                            sentinel_table="publication_outbox", base_files=PUBLICATION_BASE_FILES),
}

# Files whose statements change rows, not only structure, must say how to tell afterwards that the rows are right:
# read-only SQL returning one boolean, true when the data step has been done. Taking over a database as already
# migrated (--adopt-baseline) is refused at a data file without a check, or whose check is false.
POSTCONDITIONS = {
    "V028__index_worker.sql": (
        "SELECT NOT EXISTS (SELECT 1 FROM index_publications WHERE piece IS NOT NULL AND piece_key = '')",
    ),
    "V031__index_scheduling.sql": (
        "SELECT NOT EXISTS (SELECT 1 FROM index_publications WHERE sent_chars IS NULL AND piece IS NOT NULL "
        "AND piece ? 'chars' AND piece ? 'bytes')",
        "SELECT NOT EXISTS (SELECT 1 FROM index_publications p JOIN derived_clean_text c "
        "ON c.version_id = p.version_id AND c.status = 'parsed' "
        "AND c.processor_version || ':' || c.text_sha256 = p.artifact_key "
        "WHERE p.sent_chars IS NULL AND p.piece IS NULL)",
    ),
    "V036__history_source_lifecycle.sql": (
        "SELECT EXISTS (SELECT 1 FROM history_source_lifecycle WHERE source_id = 'legacy_documents' "
        "AND event = 'retirement_decided')",
    ),
}


def role_of(name: str) -> list[str]:
    """The roles whose manifest holds this file."""
    return [role for role, manifest in MANIFESTS.items() if name in manifest.files]


def version_files() -> list[str]:
    return sorted(path.name for path in VERSIONS_DIR.glob("V*.sql"))
