"""Real PostgreSQL fault recovery in an isolated schema; no upstream network or public writes."""

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, current_thread
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from biz_rules.collected_document import collected_document
from biz_rules.history_models import ArchiveImport, ArchiveRecord, HistoryError, canonical
from core_engine.weknora_index_client import IndexServiceError, publication_id
from db import history, index_publication as index
from integration_history import archive, raw_record
from migration import history as history_migration
from migration import index_publication as index_migration


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL URL required")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.port == 18436
    assert parsed.database == "fabric_history"
    admin = create_engine(url)
    schema = "test_index_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text("CREATE SCHEMA " + schema))
    scoped = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    try:
        yield scoped
    finally:
        scoped.dispose()
        with admin.begin() as conn:
            conn.execute(text("DROP SCHEMA " + schema + " CASCADE"))
        admin.dispose()


@pytest.fixture
def pg(pg_engine, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["external_archive","legacy_documents","clinicaltrials"]')
    monkeypatch.setattr(history, "history_engine", lambda: pg_engine)
    monkeypatch.setattr(history_migration, "history_engine", lambda: pg_engine)
    monkeypatch.setattr(index_migration, "history_engine", lambda: pg_engine)
    history_migration.migrate_history()
    index_migration.migrate_index()
    with pg_engine.begin() as conn:
        assert conn.execute(text("SELECT current_schema()")).scalar_one().startswith("test_index_")
        conn.execute(text("TRUNCATE index_publications,index_targets,history_versions,history_runs"))
        conn.execute(text("UPDATE history_sources SET published_seq=0,read_enabled=true"))
    set_targets(monkeypatch)
    return pg_engine


def set_targets(monkeypatch, *, source_id="external_archive"):
    config = [{"source_instance": "synthetic.index.pg", "source_id": source_id,
               "is_demo": True, "knowledge_base_id": "synthetic-isolated-kb", "source_scope": "research"}]
    monkeypatch.setenv("DATAFABRIC_INDEX_TARGETS", canonical(config))
    return config


def rows(pg, query, params=None):
    with pg.begin() as conn:
        return [dict(row) for row in conn.execute(text(query), params or {}).mappings()]


def job(pg, version=None):
    values = rows(pg, "SELECT * FROM index_publications" + (" WHERE version_id=:v" if version else ""), {"v": version})
    assert len(values) == 1
    return values[0]


def due(pg, version=None):
    with pg.begin() as conn:
        if version:
            conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()+interval '1 day'"))
            conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp() WHERE version_id=:v"), {"v": version})
        else:
            conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()"))


def add_document(native="SYNTHETIC-index", *, available="2019-01-01", phase="PHASE1"):
    return history.import_archive(archive([raw_record(native, phase)], available), "index-" + uuid4().hex).version_ids[0]


def add_collected(*, withdrawn=False):
    snapshot = {"uuid": "SYNTHETIC-collected", "title": "Synthetic publication withdrawal", "raw_content": "Synthetic authorized original narrative.",
                "source_site": "clinicaltrials", "source_url": "https://example.org/synthetic", "is_deleted": withdrawn}
    value = collected_document(snapshot, datetime.now(timezone.utc), "insert")
    request = ArchiveImport(source_id="legacy_documents", is_demo=True, scope_description="synthetic fixed-version withdrawal test", declared_count=1, declared_complete=False,
                            records=[ArchiveRecord(source_record_id="legacy:clinicaltrials:SYNTHETIC-collected", record_kind="document", raw_content=value.model_dump_json())])
    return history.import_archive(request, "collected-" + uuid4().hex).version_ids[0]


def receipt_for(body):
    return {"publication_id": publication_id(body), "knowledge_id": "knowledge-" + publication_id(body),
            "knowledge_base_id": body["knowledge_base_id"], "source_scope": body["source_scope"],
            "identity": {key: body[key] for key in ("source_instance", "source_id", "source_record_id", "version_id", "raw_sha256")},
            "raw_locator": copy.deepcopy(body["raw_locator"]), "content_sha256": body["content_sha256"],
            "upload_status": "uploaded", "parse_status": "completed", "vector_status": "ready", "index_status": "ready",
            "binding_status": "bound", "source_status": "active", "chunk_count": 1, "vector_count": 1}


class ProcessStopped(BaseException):
    pass


class JournalClient:
    """A service journal keyed by publication ID, with explicit transport fault points."""
    def __init__(self):
        self.journal = {}
        self.events = []
        self.bodies = []
        self.upload_count = 0
        self.after_commit = None
        self.hide_next_status = False
        self.disable_parse = "completed"
        self.disable_count = 0

    def status(self, pid):
        self.events.append(("get", pid))
        if self.hide_next_status:
            self.hide_next_status = False
            return None
        return copy.deepcopy(self.journal.get(pid))

    def publish(self, body):
        pid = publication_id(body)
        self.events.append(("post", pid))
        self.bodies.append(copy.deepcopy(body))
        if pid not in self.journal:
            self.journal[pid] = receipt_for(body)
            self.upload_count += 1
        if self.after_commit is not None:
            failure, self.after_commit = self.after_commit, None
            raise failure
        return copy.deepcopy(self.journal[pid])

    def disable(self, pid, reason):
        self.events.append(("disable", pid, reason))
        self.disable_count += 1
        self.journal[pid].update(source_status="disabled", index_status="disabled", vector_status="disabled", vector_count=0,
                                 parse_status=self.disable_parse)
        return copy.deepcopy(self.journal[pid])

    def close(self):
        pass


def ready_job(pg):
    version = add_document()
    index.scan_committed()
    client = JournalClient()
    assert index.process_once(client=client)["state"] == "ready"
    return version, client


def test_scan_and_cursor_rollback_together_then_replay_without_duplicates(pg):
    index.scan_committed()
    versions = [add_document("SYNTHETIC-A"), add_document("SYNTHETIC-B")]
    inserts = 0

    def fail_second_insert(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserts
        if "INSERT INTO index_publications" in statement:
            inserts += 1
            if inserts == 2:
                raise RuntimeError("synthetic interruption before second durable task")

    event.listen(pg, "before_cursor_execute", fail_second_insert)
    try:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            index.scan_committed()
    finally:
        event.remove(pg, "before_cursor_execute", fail_second_insert)
    assert rows(pg, "SELECT * FROM index_publications") == []
    assert rows(pg, "SELECT scanned_seq FROM index_targets")[0]["scanned_seq"] == 0
    index.scan_committed()
    assert {row["version_id"] for row in rows(pg, "SELECT version_id FROM index_publications")} == set(versions)
    assert rows(pg, "SELECT scanned_seq FROM index_targets")[0]["scanned_seq"] == 2
    index.scan_committed()
    assert len(rows(pg, "SELECT * FROM index_publications")) == 2


def test_uncommitted_version_is_invisible_and_does_not_advance_cursor(pg):
    index.scan_committed()
    before_commit, release = Event(), Event()

    def pause_writer_commit(conn):
        if current_thread().name.startswith("index-writer"):
            before_commit.set()
            assert release.wait(10), "reader did not release synthetic source transaction"

    event.listen(pg, "commit", pause_writer_commit)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="index-writer") as pool:
            pending = pool.submit(add_document)
            try:
                assert before_commit.wait(5)
                index.scan_committed()
                assert rows(pg, "SELECT * FROM index_publications") == []
                assert rows(pg, "SELECT scanned_seq FROM index_targets")[0]["scanned_seq"] == 0
            finally:
                release.set()
            version = pending.result(timeout=10)
    finally:
        release.set()
        event.remove(pg, "commit", pause_writer_commit)
    index.scan_committed()
    assert job(pg)["version_id"] == version
    assert rows(pg, "SELECT scanned_seq FROM index_targets")[0]["scanned_seq"] == 1


def test_unknown_committed_post_reconciles_before_replay_and_keeps_one_knowledge(pg):
    add_document()
    index.scan_committed()
    client = JournalClient()
    client.after_commit = IndexServiceError("index_timeout", "synthetic lost ACK", 504)
    assert index.process_once(client=client)["state"] == "waiting"
    first = job(pg)
    assert first["first_send_at"] is not None
    due(pg)
    assert index.process_once(client=client)["state"] == "ready"
    assert [event[0] for event in client.events] == ["get", "post", "get"]
    assert client.upload_count == 1
    assert job(pg)["knowledge_id"]
    due(pg)
    assert index.process_once(client=client)["state"] == "ready"
    assert client.upload_count == 1


def test_active_missing_receipt_replays_only_same_reserved_identity_after_get(pg):
    add_document()
    index.scan_committed()
    client = JournalClient()
    client.after_commit = IndexServiceError("index_timeout", "synthetic lost ACK", 504)
    assert index.process_once(client=client)["state"] == "waiting"
    client.hide_next_status = True
    due(pg)
    assert index.process_once(client=client)["state"] == "ready"
    assert [event[0] for event in client.events] == ["get", "post", "get", "post"]
    assert client.bodies[0] == client.bodies[1]
    assert client.upload_count == 1


def test_process_interrupt_preserves_send_intent_and_expired_lease_recovers(pg):
    add_document()
    index.scan_committed()
    client = JournalClient()
    client.after_commit = ProcessStopped("synthetic abrupt worker exit after remote commit")
    with pytest.raises(ProcessStopped):
        index.process_once(client=client)
    saved = job(pg)
    assert saved["first_send_at"] is not None and saved["state"] == "sending"
    assert saved["lease_token"]
    assert index.process_once(client=client)["state"] == "idle"
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET lease_until=clock_timestamp()-interval '1 second'"))
    assert index.process_once(client=client)["state"] == "ready"
    assert client.upload_count == 1
    assert job(pg)["lease_token"] is None


def test_expired_worker_cannot_send_or_confirm_new_owners_lease(pg):
    add_document()
    index.scan_committed()
    stale = index._claim()
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET lease_until=clock_timestamp()-interval '1 second'"))
    current = index._claim()
    assert stale["lease_token"] != current["lease_token"]
    for operation in (lambda: index._sent(stale), lambda: index._finish(stale, "ready", "active")):
        with pytest.raises(HistoryError) as error:
            operation()
        assert error.value.code == "index_lease_lost"
    assert job(pg)["lease_token"] == current["lease_token"]
    index._finish(current, "indexing", "active")
    assert job(pg)["lease_token"] is None


def test_revocation_waits_for_terminal_parse_and_keeps_auditing_after_disable(pg):
    version, client = ready_job(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"))
    client.disable_parse = "processing"
    due(pg)
    result = index.process_once(client=client)
    assert result["state"] == "disabling" and result["desired_state"] == "revoked"
    client.disable_parse = "completed"
    due(pg)
    assert index.process_once(client=client)["state"] == "disabled"
    pid = job(pg)["remote_id"]
    client.journal[pid].update(index_status="pending", vector_status="ready", vector_count=2)
    due(pg)
    assert index.process_once(client=client)["state"] == "disabled"
    assert client.disable_count == 3
    assert job(pg)["receipt"]["vector_count"] == 0


def test_revocation_during_remote_upload_cannot_confirm_ready(pg):
    add_document()
    index.scan_committed()
    client = JournalClient()
    original_publish = client.publish

    def publish_then_revoke(body):
        receipt = original_publish(body)
        with pg.begin() as conn:
            conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"))
        return receipt

    client.publish = publish_then_revoke
    first = index.process_once(client=client)
    assert first["state"] == "disabling" and first["desired_state"] == "revoked"
    assert job(pg)["receipt"]["source_status"] == "active"
    due(pg)
    assert index.process_once(client=client)["state"] == "disabled"
    assert client.upload_count == 1 and client.disable_count == 1


def test_blocked_job_does_no_http_until_revocation_requires_cleanup(pg):
    version, client = ready_job(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET state='blocked',attempts=6,desired_state='active',error='{}'::jsonb"))
    before = len(client.events)
    due(pg)
    index.process_once(client=client)
    assert len(client.events) == before
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"))
    due(pg)
    result = index.process_once(client=client)
    assert result["state"] == "disabled" and result["desired_state"] == "revoked"
    assert client.disable_count == 1


def test_cleanup_remains_due_after_retry_limit_and_recovers_when_service_returns(pg):
    version, client = ready_job(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"))
    original_disable = client.disable

    def unavailable_disable(pid, reason):
        raise IndexServiceError("index_unavailable", "synthetic temporary cleanup outage", 503)

    client.disable = unavailable_disable
    for _ in range(6):
        due(pg)
        result = index.process_once(client=client)
    assert result["state"] == "blocked" and result["desired_state"] == "revoked"
    assert client.journal[job(pg)["remote_id"]]["source_status"] == "active"
    client.disable = original_disable
    due(pg)
    recovered = index.process_once(client=client)
    assert recovered["state"] == "disabled"
    assert job(pg)["receipt"]["source_status"] == "disabled"


def test_removing_environment_target_keeps_cleanup_for_existing_publication(pg, monkeypatch):
    version, client = ready_job(pg)
    monkeypatch.delenv("DATAFABRIC_INDEX_TARGETS")
    monkeypatch.setattr(index, "WeKnoraIndexClient", lambda: client)
    due(pg)
    index.index_tick(batch_size=1)
    assert rows(pg, "SELECT enabled FROM index_targets")[0]["enabled"] is False
    assert job(pg)["state"] == "disabled" and job(pg)["desired_state"] == "revoked"
    assert client.disable_count == 1


def test_late_older_archive_does_not_supersede_head_but_new_revision_does(pg):
    newer = add_document(available="2020-01-01", phase="PHASE2")
    older = add_document(available="2010-01-01", phase="PHASE1")
    index.scan_committed()
    client = JournalClient()
    due(pg, older)
    assert index.process_once(client=client)["desired_state"] == "superseded"
    assert client.upload_count == 0
    due(pg, newer)
    assert index.process_once(client=client)["state"] == "ready"
    corrected = add_document(available="2021-01-01", phase="PHASE3")
    index.scan_committed()
    due(pg, newer)
    assert index.process_once(client=client)["desired_state"] == "superseded"
    assert client.disable_count == 1
    due(pg, corrected)
    assert index.process_once(client=client)["state"] == "ready"
    assert client.upload_count == 2


def test_withdrawal_tombstone_disables_prior_index_without_uploading_empty_body(pg, monkeypatch):
    set_targets(monkeypatch, source_id="legacy_documents")
    active = add_collected()
    index.scan_committed()
    client = JournalClient()
    assert index.process_once(client=client)["state"] == "ready"
    withdrawn = add_collected(withdrawn=True)
    index.scan_committed()
    due(pg, active)
    assert index.process_once(client=client)["desired_state"] == "superseded"
    due(pg, withdrawn)
    result = index.process_once(client=client)
    assert result["state"] == "disabled" and result["desired_state"] == "withdrawn"
    assert client.upload_count == 1 and client.disable_count == 1


def test_sent_then_revoked_missing_remote_is_not_confirmed_disabled(pg):
    add_document()
    index.scan_committed()
    claimed = index._claim()
    index._sent(claimed)
    index._finish(claimed, "waiting", "active")
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"))
    client = JournalClient()
    due(pg)
    result = index.process_once(client=client)
    assert result["state"] == "waiting" and result["desired_state"] == "revoked"
    assert job(pg)["error"]["code"] == "index_disable_unknown"
    assert [entry[0] for entry in client.events] == ["get"]


def test_conflicting_receipt_is_rejected_before_any_recovery_post(pg):
    add_document()
    index.scan_committed()
    saved = job(pg)
    wrong = receipt_for(saved["manifest"])
    wrong.update(binding_status="pending", knowledge_id=None)
    wrong["identity"]["raw_sha256"] = "f" * 64
    client = JournalClient()
    client.journal[saved["remote_id"]] = wrong
    result = index.process_once(client=client)
    assert result["state"] == "waiting"
    assert job(pg)["error"]["code"] == "index_binding_mismatch"
    assert [entry[0] for entry in client.events] == ["get"]


def test_a_sent_job_is_confirmed_even_behind_a_backlog_of_new_uploads(pg):
    """Live 2026-11-03: thousands of queued uploads were due earlier, so sent jobs waiting 15 s for their
    check never came up again and nothing turned ready."""
    class Parsing(JournalClient):
        parsing = True

        def publish(self, body):
            value = super().publish(body)
            if self.parsing:  # the first answer: uploaded, the upstream parse not finished yet
                self.journal[publication_id(body)].update(
                    parse_status="pending", vector_status="pending", index_status="pending")
                value = copy.deepcopy(self.journal[publication_id(body)])
            return value

    client = Parsing()
    first = add_document("SYNTHETIC-sent")
    index.scan_committed()
    assert index.process_once(client=client)["state"] == "indexing"
    client.parsing = False
    client.journal[publication_id(client.bodies[0])].update(receipt_for(client.bodies[0]))  # parsed now
    for i in range(30):
        add_document(f"SYNTHETIC-backlog-{i}")
    index.scan_committed(batch_size=100)
    with pg.begin() as conn:  # the backlog became due before the sent job's next check
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()-interval '1 hour' WHERE version_id<>:v"), {"v": first})
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp() WHERE version_id=:v"), {"v": first})
    assert index.process_once(client=client, sent=True)["state"] == "ready"
    assert job(pg, first)["state"] == "ready"
    assert index.process_once(client=client, sent=False)["state"] in {"ready", "indexing"}  # new uploads still move
