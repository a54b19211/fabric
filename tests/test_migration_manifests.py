"""The migration manifests: explicit, disjoint where they must be, and every file declared."""

import shutil
import subprocess
from pathlib import Path

import pytest

from store.migrations import manifests
from store.migrations.manifests import MANIFESTS, POSTCONDITIONS, role_of, version_files
from store.migrations.sqlcheck import inspect_sql

REPO = Path(__file__).resolve().parents[1]
LAST_COMPAT_VERSION = 35
# the store/migrations/versions/ listing of 7337e54, written out again here on purpose: the manifest tuple is compared with
# this list, not with itself
V001_TO_V035 = [
    "V001__init_schema.sql", "V002__add_missing_columns.sql", "V003__alter_embedding_dim.sql",
    "V004__add_merge_queue.sql", "V005__add_weknora_graph_status.sql", "V006__add_operation_log.sql",
    "V007__add_merge_queue_missing_columns.sql", "V008__add_alias_columns.sql", "V009__add_agent_run.sql",
    "V010__add_agent_run_context.sql", "V011__add_agent_run_document.sql",
    "V012__add_document_pending_entity_count.sql", "V013__add_identity_merge_proposal.sql",
    "V014__add_entity_merge_log.sql", "V015__add_name_embedding_column.sql", "V016__add_lineage.sql",
    "V017__history_records.sql", "V018__graph_record_kind.sql", "V019__publication_outbox.sql",
    "V020__weknora_index.sql", "V021__history_version_rank.sql", "V022__registry_changes.sql",
    "V023__derived_clean_text.sql", "V024__index_artifact.sql", "V025__clean_stage.sql",
    "V026__history_extraction_subjects.sql", "V027__index_publications_recent.sql", "V028__index_worker.sql",
    "V029__legacy_aliases.sql", "V030__profile_annotations.sql", "V031__index_scheduling.sql",
    "V032__extraction_checks.sql", "V033__doc_authority_resolution.sql", "V034__doc_authority_skipped.sql",
    "V035__identity_evidence.sql",
]


def number(name):
    return int(name[1:4])


def text_of(name):
    return (manifests.VERSIONS_DIR / name).read_text(encoding="utf-8")


def test_the_app_compatibility_list_is_the_35_files_of_7337e54():
    assert list(manifests.APP_COMPAT_FILES) == V001_TO_V035
    if shutil.which("git") and subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", "7337e54"]).returncode == 0:
        listing = subprocess.run(["git", "-C", str(REPO), "ls-tree", "--name-only", "7337e54", "migration/versions/"],
                                 capture_output=True, text=True, check=True).stdout.split()
        assert [Path(p).name for p in listing] == V001_TO_V035


def test_every_file_belongs_to_at_least_one_role():
    assert [name for name in version_files() if not role_of(name)] == []


def test_a_file_after_v035_belongs_to_exactly_one_role():
    for name in version_files():
        if number(name) > LAST_COMPAT_VERSION:
            assert len(role_of(name)) == 1, name


def test_the_manifests_only_name_files_that_exist_and_do_not_repeat_them():
    existing = set(version_files())
    for manifest in MANIFESTS.values():
        assert set(manifest.files) <= existing
        assert len(set(manifest.files)) == len(manifest.files)
        assert list(manifest.files) == sorted(manifest.files)


def test_history_and_publication_do_not_overlap_and_are_inside_the_app_compatibility_list():
    history, publication = set(MANIFESTS["history"].files), set(MANIFESTS["publication"].files)
    assert not history & publication
    for name in history | publication:
        if number(name) <= LAST_COMPAT_VERSION:
            assert name in manifests.APP_COMPAT_FILES


def test_the_history_manifest_is_the_union_of_the_two_former_runners():
    first = {"V017", "V018", "V021", "V022", "V023", "V025", "V029", "V030", "V032"}  # migration/history.py
    second = {"V020", "V024", "V027", "V028", "V031"}  # run/index.py
    assert {name[:4] for name in MANIFESTS["history"].files if number(name) <= LAST_COMPAT_VERSION} == first | second
    assert {name[:4] for name in MANIFESTS["publication"].files} == {"V019", "V026"}  # the publication role


def test_only_the_four_object_library_files_carry_their_own_transaction_control():
    carrying = {name for name in version_files() if any("top-level" in p for p in inspect_sql(text_of(name)).problems)}
    assert carrying == set(manifests.TRANSACTION_CONTROL_ALLOWED)


def test_a_data_file_of_the_history_and_publication_roles_declares_postconditions():
    for role in ("history", "publication"):
        for name in MANIFESTS[role].files:
            report = inspect_sql(text_of(name))
            assert report.problems == (), (name, report.problems)
            if report.data_statements:
                assert POSTCONDITIONS.get(name), f"{name} changes rows and declares no postcondition"
    assert set(POSTCONDITIONS) <= {n for m in MANIFESTS.values() for n in m.files}


def test_every_declared_postcondition_belongs_to_a_file_that_changes_rows():
    for name in POSTCONDITIONS:
        assert inspect_sql(text_of(name)).data_statements, name


@pytest.mark.parametrize("name", V001_TO_V035)
def test_the_checks_accept_every_existing_file_except_the_four_with_transaction_control(name):
    report = inspect_sql(text_of(name), allow_transaction_control=name in manifests.TRANSACTION_CONTROL_ALLOWED)
    assert report.problems == (), name
