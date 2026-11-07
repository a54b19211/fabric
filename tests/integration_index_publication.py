"""Real PostgreSQL fault recovery and retrieval checks (v3 3.6) in an isolated schema; no upstream network."""

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, current_thread
from uuid import uuid4

import legacy_bridge
import pytest
from integration_history import archive, raw_record
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from biz_rules.collected_document import collected_document
from biz_rules.history_models import ArchiveImport, ArchiveRecord, HistoryError, canonical
from core_engine.weknora_index_client import IndexServiceError, publication_id
from db import clean_stage, history, retrieval
from db import index_publication as index
from migration import history as history_migration
from migration import index_publication as index_migration


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL URL required")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1"
    assert (parsed.port, parsed.database) in {
        (18436, "fabric_history"), (18636, "bioradar_kdd_opus_history"),
    }
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
    legacy_bridge.open_old_writer(monkeypatch)  # withdrawal fixtures are legacy versions (cutover mode)
    monkeypatch.setattr(history, "history_engine", lambda: pg_engine)
    monkeypatch.setattr(history_migration, "history_engine", lambda: pg_engine)
    monkeypatch.setattr(index_migration, "history_engine", lambda: pg_engine)
    history_migration.migrate_history()
    index_migration.migrate_index()
    with pg_engine.begin() as conn:
        assert conn.execute(text("SELECT current_schema()")).scalar_one().startswith("test_index_")
        conn.execute(text("TRUNCATE derived_stage_chunks,derived_stage_waiting,derived_identifiers,derived_profiles,derived_text_claims,history_attachment_links,derived_clean_text,derived_registry_changes,derived_registry_diff_runs,index_publications,index_targets,index_claim_cursor,index_send_bucket,index_gate_stats,derived_extraction_check_tasks,derived_extraction_checks,history_versions,history_runs"))
        conn.execute(text("UPDATE history_sources SET published_seq=0,read_enabled=true"))
    set_targets(monkeypatch)
    return pg_engine


def set_targets(monkeypatch, *, source_id="external_archive"):
    config = [{"source_instance": "synthetic.index.pg", "source_id": source_id,
               "is_demo": True, "knowledge_base_id": "synthetic-isolated-kb", "source_scope": "research"}]
    monkeypatch.setenv("DATAFABRIC_INDEX_TARGETS", canonical(config))
    return config


def set_old_targets(monkeypatch):
    """The target of an index built before the retirement (legacy_documents). The configuration loader refuses such a
    target now (index_target_retired_source), so the tests of withdrawal and revocation on those old entries install the
    target object directly."""
    target = index.IndexTarget(source_instance="synthetic.index.pg", source_id="legacy_documents", is_demo=True,
                               knowledge_base_id="synthetic-isolated-kb", source_scope="research")
    monkeypatch.setattr(index, "targets", lambda: [target])


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


def add_document(native="SYNTHETIC-index", *, available="2019-01-01", phase="PHASE1", **extra):
    return history.import_archive(archive([raw_record(native, phase, **extra)], available),
                                  "index-" + uuid4().hex).version_ids[0]


def add_collected(*, withdrawn=False):
    snapshot = {"uuid": "SYNTHETIC-collected", "title": "Synthetic publication withdrawal", "raw_content": "Synthetic authorized original narrative.",
                "source_site": "clinicaltrials", "source_url": "https://example.org/synthetic", "is_deleted": withdrawn}
    value = collected_document(snapshot, datetime.now(timezone.utc), "insert")
    request = ArchiveImport(source_id="legacy_documents", is_demo=True, scope_description="synthetic fixed-version withdrawal test", declared_count=1, declared_complete=False,
                            records=[ArchiveRecord(source_record_id="legacy:clinicaltrials:SYNTHETIC-collected", record_kind="document", raw_content=value.model_dump_json())])
    return history.import_archive(request, "collected-" + uuid4().hex).version_ids[0]


def profile():
    """清洗段画像；画像时同一事务入队（前提是索引目标已同步过）。"""
    return clean_stage.process(time_budget_seconds=60)


def scan(batch_size=100):
    """先画像（新增入队），再做回填扫描（已画像而没有待办的版本）。"""
    profile()
    return index.scan_committed(batch_size=batch_size)


def due_job(pg, job_id):
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()+interval '1 day'"))
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp() WHERE id=:id"), {"id": job_id})


def receipt_for(body):
    return {"publication_id": publication_id(body), "is_demo": body["is_demo"],
            "knowledge_id": "knowledge-" + publication_id(body),
            "knowledge_base_id": body["knowledge_base_id"], "source_scope": body["source_scope"],
            "identity": {key: body[key] for key in ("source_instance", "source_id", "source_record_id", "version_id",
                                                    "raw_sha256", "artifact_key", "piece") if key in body},
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
    scan()
    client = JournalClient()
    assert index.process_once(client=client)["state"] == "ready"
    return version, client


def test_scan_and_cursor_rollback_together_then_replay_without_duplicates(pg):
    versions = [add_document("SYNTHETIC-A"), add_document("SYNTHETIC-B")]
    profile()  # 目标还没同步：画像不入队，回填扫描负责
    assert rows(pg, "SELECT * FROM index_publications") == []
    with pg.begin() as conn:
        index._sync_targets(conn, index.targets())
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
    scan()
    assert job(pg)["version_id"] == version
    assert rows(pg, "SELECT scanned_seq FROM index_targets")[0]["scanned_seq"] == 1


def test_unknown_committed_post_reconciles_before_replay_and_keeps_one_knowledge(pg):
    add_document()
    scan()
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
    scan()
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
    scan()
    client = JournalClient()
    client.after_commit = ProcessStopped("synthetic abrupt worker exit after remote commit")
    with pytest.raises(ProcessStopped):
        index.process_once(client=client)
    saved = job(pg)
    assert saved["first_send_at"] is not None and saved["state"] == "sending"
    assert saved["lease_token"]
    assert index.process_once(client=client)["state"] == "idle"
    with pg.begin() as conn:  # the lease's time has passed (claiming moved the next check to the lease's end)
        conn.execute(text("UPDATE index_publications SET lease_until=clock_timestamp()-interval '1 second',"
                          "next_check_at=clock_timestamp()-interval '1 second'"))
    assert index.process_once(client=client)["state"] == "ready"
    assert client.upload_count == 1
    assert job(pg)["lease_token"] is None


def test_expired_worker_cannot_send_or_confirm_new_owners_lease(pg):
    add_document()
    scan()
    stale = index._claim()
    with pg.begin() as conn:  # the lease's time has passed (claiming moved the next check to the lease's end)
        conn.execute(text("UPDATE index_publications SET lease_until=clock_timestamp()-interval '1 second',"
                          "next_check_at=clock_timestamp()-interval '1 second'"))
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
    scan()
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
    index.scan_committed()
    index.process_once()
    assert rows(pg, "SELECT enabled FROM index_targets")[0]["enabled"] is False
    assert job(pg)["state"] == "disabled" and job(pg)["desired_state"] == "revoked"
    assert client.disable_count == 1


def test_corrections_keep_history_indexed_and_each_text_is_indexed_once(pg):
    """v3 3.6 / Codex 429：更正不停用旧正文（旧截止时间仍要检索得到）；清洗正文相同的版本只建一条。"""
    newer = add_document(available="2020-01-01", title="Synthetic trial, 2020 wording")
    older = add_document(available="2010-01-01", title="Synthetic trial, 2010 wording")
    scan()
    client = JournalClient()
    for _ in range(2):
        assert index.process_once(client=client)["state"] == "ready"
    corrected = add_document(available="2021-01-01", title="Synthetic trial, corrected wording")
    scan()
    due(pg)
    for _ in range(3):
        index.process_once(client=client)
    states = {r["version_id"]: r["state"] for r in rows(pg, "SELECT version_id,state FROM index_publications")}
    assert states == {newer: "ready", older: "ready", corrected: "ready"}
    assert client.disable_count == 0 and client.upload_count == 3
    # 同一记录：正文相同的版本只建一条，代表版本是最早产出这份正文的版本
    versions = [r["version_id"] for r in rows(pg, "SELECT version_id FROM history_versions ORDER BY published_seq")]
    with pg.begin() as conn:
        keys = {v: index.artifact_key(a) for v, a in index.current_artifacts(conn, versions).items()}
    assert len(rows(pg, "SELECT * FROM index_publications")) == len(set(keys.values()))


def test_same_text_versions_share_one_entry_with_a_fixed_representative(pg):
    first = add_document("SYNTHETIC-same", available="2010-01-01", phase="PHASE1")
    second = add_document("SYNTHETIC-same", available="2012-01-01", phase="PHASE2")  # 原件变了，清洗正文没变
    assert first != second
    scan()
    with pg.begin() as conn:
        artifacts = index.current_artifacts(conn, [first, second])
    assert index.artifact_key(artifacts[first]) == index.artifact_key(artifacts[second])
    entries = rows(pg, "SELECT version_id,artifact_key FROM index_publications")
    assert [e["version_id"] for e in entries] == [first]  # 一条，代表版本固定为最早的
    scan()  # 再扫描、再画像不改已有条目
    assert rows(pg, "SELECT version_id,artifact_key FROM index_publications") == entries


def test_withdrawal_does_not_disable_the_indexed_text(pg, monkeypatch):
    """撤回是一个新版本：当前模式下检索时剔除（withdrawn），撤回之前的截止时间仍可检索；索引条目保留。"""
    set_old_targets(monkeypatch)
    active = add_collected()
    scan()
    client = JournalClient()
    assert index.process_once(client=client)["state"] == "ready"
    add_collected(withdrawn=True)
    scan()
    due(pg)
    index.process_once(client=client)
    assert job(pg, active)["state"] == "ready"
    assert client.upload_count == 1 and client.disable_count == 0


def test_sent_then_revoked_missing_remote_is_not_confirmed_disabled(pg):
    add_document()
    scan()
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
    scan()
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
    scan()
    assert index.process_once(client=client)["state"] == "indexing"
    client.parsing = False
    client.journal[publication_id(client.bodies[0])].update(receipt_for(client.bodies[0]))  # parsed now
    for i in range(30):
        add_document(f"SYNTHETIC-backlog-{i}")
    scan(batch_size=100)
    with pg.begin() as conn:  # the backlog became due before the sent job's next check
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()-interval '1 hour' WHERE version_id<>:v"), {"v": first})
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp() WHERE version_id=:v"), {"v": first})
    assert index.process_once(client=client, lane="new", sent=True)["state"] == "ready"
    assert job(pg, first)["state"] == "ready"
    assert index.process_once(client=client, lane="new", sent=False)["state"] in {"ready", "indexing"}


def test_profile_enqueues_in_the_same_transaction_with_search_metadata(pg):
    """v3 3.6：清洗正文合格就入队（画像同一事务），清单带检索元数据；发给桥接的只有它认识的字段。"""
    index.scan_committed()  # 同步目标
    version = add_document("SYNTHETIC-meta", available="2019-01-01")
    assert rows(pg, "SELECT * FROM index_publications") == []
    counts = profile()
    assert counts["index_enqueued"] == 1
    saved = job(pg, version)
    meta = saved["manifest"]["metadata"]
    from processing import clean_text
    from processing import profile as profile_module
    assert saved["lane"] == "new" and saved["created_at"] is not None
    assert saved["artifact_key"].startswith(clean_text.PROCESSOR_VERSION + ":")
    assert saved["manifest"]["content_sha256"] == meta["artifact_sha256"] == saved["artifact_key"].split(":")[1]
    assert meta["source_id"] == "external_archive" and meta["version_id"] == version
    assert meta["processor_version"] == clean_text.PROCESSOR_VERSION and meta["first_seen_at"]
    assert meta["proof_kind"] == "reviewed_archive" and meta["proof_available_at"].startswith("2019-01-01")
    assert meta["doc_type"] and "doc_category" in meta
    with pg.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM derived_profiles WHERE version_id=:v AND processor_version=:p"),
                            {"v": version, "p": profile_module.PROCESSOR_VERSION}).scalar_one() == 1
    client = JournalClient()
    assert index.process_once(client=client)["state"] == "ready"
    assert set(client.bodies[0]) <= set(index.BRIDGE_FIELDS) | {"title", "content"}
    assert client.bodies[0]["is_demo"] is True  # 演示目标：桥接按它隔离演示与真实知识库
    assert client.bodies[0]["metadata"] == meta
    assert job(pg, version)["ready_at"] is not None


def test_enqueue_failure_rolls_back_the_profile_and_retries_cleanly(pg):
    index.scan_committed()
    add_document("SYNTHETIC-rollback")

    def fail_enqueue(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO index_publications" in statement:
            raise RuntimeError("synthetic enqueue failure")

    event.listen(pg, "before_cursor_execute", fail_enqueue)
    try:
        assert profile()["failed_chunks"] >= 1  # 块重试到上限后进死信
    finally:
        event.remove(pg, "before_cursor_execute", fail_enqueue)
    assert rows(pg, "SELECT * FROM derived_profiles") == [] and rows(pg, "SELECT * FROM index_publications") == []
    clean_stage.retry_failed()
    assert profile()["profiled"] == 1
    assert len(rows(pg, "SELECT * FROM index_publications")) == 1


def test_clean_processor_upgrade_hands_over_only_after_the_new_entry_is_ready(pg, monkeypatch):
    """Codex 429/431：新产物能检索后才停旧条目，不撤掉最后的入口；旧记录保留。"""
    from processing import clean_text

    version, client = ready_job(pg)
    old = job(pg)
    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "synthetic.clean_text.v99")
    profile()
    new = next(r for r in rows(pg, "SELECT * FROM index_publications") if r["id"] != old["id"])
    assert new["artifact_key"].startswith("synthetic.clean_text.v99:") and new["version_id"] == version
    due_job(pg, old["id"])
    assert index.process_once(client=client)["state"] == "ready"  # 新的还没 ready：旧的保留（retiring，不联网）
    assert client.disable_count == 0
    assert rows(pg, "SELECT state FROM index_publications WHERE id=:i", {"i": old["id"]})[0]["state"] == "ready"
    due_job(pg, new["id"])
    assert index.process_once(client=client)["state"] == "ready"
    # 新条目刚 ready：旧条目马上复查，判 superseded 并停用
    assert index.process_once(client=client)["desired_state"] == "superseded"
    states = {r["id"]: (r["state"], r["desired_state"]) for r in rows(pg, "SELECT * FROM index_publications")}
    assert states == {old["id"]: ("disabled", "superseded"), new["id"]: ("ready", "active")}
    assert client.disable_count == 1


def test_pre_36_projection_entries_hand_over_and_never_sent_ones_stay_local(pg):
    """改造前按投影正文建的待办（artifact_key 为空）：r72 有 ready 而 first_send_at 为空的行，须对账后停用；
    确认没发出过的直接停用，不联网。"""
    version = add_document("SYNTHETIC-legacy")
    scan()
    artifact_job = job(pg)
    legacy_manifest = {**artifact_job["manifest"], "content_sha256": "c" * 64}
    for key in ("artifact_key", "processor_version", "artifact_sha256", "locator_kind", "parent_record_id", "metadata"):
        legacy_manifest.pop(key, None)
    legacy_remote = publication_id(legacy_manifest)
    client = JournalClient()
    client.journal[legacy_remote] = receipt_for(legacy_manifest)
    with pg.begin() as conn:  # r72 的样子：ready、有知识编号和回执，但 first_send_at 为空
        conn.execute(text("""
            INSERT INTO index_publications (id,target_id,version_id,source_record_id,remote_id,manifest,artifact_key,
              state,knowledge_id,receipt)
            VALUES (:id,:t,:v,:n,:r,CAST(:m AS jsonb),'','ready','knowledge-legacy',CAST(:receipt AS jsonb))
        """), {"id": "d" * 64, "t": artifact_job["target_id"], "v": version, "n": artifact_job["source_record_id"],
               "r": legacy_remote, "m": canonical(legacy_manifest), "receipt": canonical(client.journal[legacy_remote])})
    due_job(pg, "d" * 64)
    before = len(client.events)
    index.process_once(client=client)  # 产物条目还没 ready：旧条目保留（retiring），不联网
    assert len(client.events) == before
    assert rows(pg, "SELECT state FROM index_publications WHERE id=:i", {"i": "d" * 64})[0]["state"] == "ready"
    due_job(pg, artifact_job["id"])
    assert index.process_once(client=client)["state"] == "ready"
    result = index.process_once(client=client)
    assert result["desired_state"] == "superseded" and result["state"] == "disabled"
    assert ("disable", legacy_remote, "superseded") in client.events


def test_never_sent_entry_is_disabled_locally_without_network(pg):
    version = add_document("SYNTHETIC-local")
    scan()
    saved = job(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='external_archive'"))
    client = JournalClient()
    result = index.process_once(client=client)
    assert result == {"id": saved["id"], "state": "disabled", "desired_state": "revoked"}
    assert client.events == []
    assert job(pg, version)["state"] == "disabled"
    # 隔离实测发现（3.6）：本地停用后每 5 分钟复查一次，复查时仍认得“从没发出过”，不去桥接对账
    for _ in range(2):
        due(pg)
        assert index.process_once(client=client)["state"] == "disabled"
    assert client.events == [] and job(pg, version)["attempts"] == 0 and job(pg, version)["error"] is None


def test_worker_threads_take_lanes_until_everything_is_ready(pg, monkeypatch):
    for i in range(6):
        add_document(f"SYNTHETIC-worker-{i}")
    scan()
    client = JournalClient()
    monkeypatch.setattr(index, "WeKnoraIndexClient", lambda: client)
    counts = index.run_worker(concurrency=2, rounds=8, idle_seconds=0, scan_every_seconds=1e9)
    assert sum(c.get("ready", 0) for c in counts) >= 6
    assert {r["state"] for r in rows(pg, "SELECT state FROM index_publications")} == {"ready"}
    assert client.upload_count == 6


def test_attachment_enters_index_only_after_parse_and_publishes_derived_text(pg, monkeypatch, tmp_path):
    """v3 3.3a/3.6：附件正文在加工层；解析成功、清洗段画像后才进索引，发布的是派生正文，回指附件版本。"""
    from integration_history import _fetch_attachment

    from db import derived_text
    from processing import attachment_parse as ap

    version, _ = _fetch_attachment(tmp_path, monkeypatch, "idx-att")
    monkeypatch.setenv("DATAFABRIC_INDEX_TARGETS", canonical([{
        "source_instance": "synthetic.index.pg", "source_id": "nmpa", "is_demo": False,
        "knowledge_base_id": "synthetic-isolated-kb", "source_scope": "research"}]))
    scan()
    assert rows(pg, "SELECT * FROM index_publications") == []  # 还没有正文：不入索引待办
    monkeypatch.setattr(ap, "_mineru", lambda p: {"success": True, "markdown": "# 附件\n\n批准文号 国药准字H00000000。", "error": ""})
    assert derived_text.process(batch=5, retry_after_seconds=0)["parsed"] == 1
    assert rows(pg, "SELECT * FROM index_publications") == []  # 解析完还要画像
    profile()
    queued = job(pg, version)
    assert queued["artifact_key"].startswith(ap.PROCESSOR_VERSION + ":")
    assert queued["manifest"]["parent_record_id"] == "https://www.nmpa.gov.cn/xxgk/ggtg/1.html"
    client = JournalClient()
    assert index.process_once(client=client)["state"] == "ready"
    assert client.bodies[-1]["content"].endswith("国药准字H00000000。")
    scan()  # 再扫描不重复建待办
    assert len(rows(pg, "SELECT * FROM index_publications")) == 1


def test_failed_attachment_parse_is_not_indexed(pg, monkeypatch, tmp_path):
    from integration_history import _fetch_attachment

    from db import derived_text
    from processing import attachment_parse as ap

    _fetch_attachment(tmp_path, monkeypatch, "idx-att-fail")
    monkeypatch.setenv("DATAFABRIC_INDEX_TARGETS", canonical([{
        "source_instance": "synthetic.index.pg", "source_id": "nmpa", "is_demo": False,
        "knowledge_base_id": "synthetic-isolated-kb", "source_scope": "research"}]))
    monkeypatch.setattr(ap, "_mineru", lambda p: {"success": False, "markdown": "", "error": "x"})
    derived_text.process(batch=5, retry_after_seconds=0)
    scan()
    assert rows(pg, "SELECT * FROM index_publications") == []


# —— 检索校验（db.retrieval）——

class SearchClient(JournalClient):
    """桥接服务的替身：召回 = 给定的命中（只来自已发布的条目）；重排记下收到的正文。"""

    def __init__(self, pg):
        super().__init__()
        self.pg, self.hits, self.reranked = pg, [], []

    def hit(self, version_or_entry, content, *, chunk="c"):
        entry = version_or_entry if isinstance(version_or_entry, dict) else job(self.pg, version_or_entry)
        manifest = entry["manifest"]
        identity = {k: manifest[k] for k in ("source_instance", "source_id", "source_record_id", "version_id",
                                             "raw_sha256", "artifact_key", "piece") if manifest.get(k)}
        self.hits.append({"chunk_id": chunk + str(len(self.hits)), "content": content, "score": 1.0 / (1 + len(self.hits)),
                          "retrieval_modes": ["keyword"], "publication_id": entry["remote_id"], "identity": identity,
                          "is_demo": True, "metadata": manifest.get("metadata")})

    def search(self, body):
        self.events.append(("search", body["knowledge_base_id"]))
        return {"results": copy.deepcopy(self.hits), "execution": {"keyword": "succeeded", "vector": "succeeded"}}

    def rerank(self, body):
        self.reranked.extend(body["documents"])
        return {"scores": [{"index": i, "relevance_score": 1.0 - i / 100} for i in range(len(body["documents"]))],
                "rerank_model": {"model_id": "synthetic"}, "usage": None, "usage_status": "unknown"}


def fixed_text(pg, version):
    with pg.begin() as conn:
        return index.current_artifacts(conn, [version])[version]["text"]


def search(client, mode="current", cutoff=None, **kw):
    return retrieval.search("phase", mode, cutoff or datetime.now(timezone.utc), client=client, **kw)


def test_retrieval_binds_each_hit_to_the_version_visible_at_the_cutoff(pg):
    v1 = add_document("SYNTHETIC-r", available="2019-01-01", title="Synthetic phase one wording")
    v2 = add_document("SYNTHETIC-r", available="2020-01-01", title="Synthetic phase two wording, corrected")
    scan()
    client = SearchClient(pg)
    t1, t2 = fixed_text(pg, v1), fixed_text(pg, v2)
    client.hit(v1, t1[:40])
    client.hit(v2, t2[:40])
    now = search(client)
    assert [(r["version_id"], r["text"]) for r in now["results"]] == [(v2, t2[:40])]
    assert now["excluded"]["counts"] == {"changed_at_cutoff": 1}
    assert now["excluded"]["items"][0]["indexed_version_id"] == v1
    past = search(client, "public_history_reconstruction", datetime(2019, 6, 1, tzinfo=timezone.utc))
    assert [r["version_id"] for r in past["results"]] == [v1]
    assert past["results"][0]["available_at"].startswith("2019-01-01")
    assert past["excluded"]["counts"] == {"changed_at_cutoff": 1}  # 那时可见的是 v1，v2 的正文还不存在
    before_any = search(client, "public_history_reconstruction", datetime(2018, 1, 1, tzinfo=timezone.utc))
    assert before_any["results"] == [] and before_any["execution"]["insufficient"]
    # 只有通过校验的片段进了重排
    assert set(client.reranked) == {t1[:40], t2[:40]}
    rerank = now["execution"]["rerank"]  # what went to the rerank model is counted in the receipt
    assert (rerank["provider_calls"], rerank["documents"], rerank["input_chars"]) == (1, 1, len(t2[:40]))
    assert (before_any["execution"]["rerank"]["state"], before_any["execution"]["rerank"]["input_chars"]) == ("not_run", 0)


def test_returned_evidence_uses_the_visible_version_not_the_representative(pg):
    first = add_document("SYNTHETIC-rep", available="2010-01-01", phase="PHASE1")
    second = add_document("SYNTHETIC-rep", available="2012-01-01", phase="PHASE2")  # 同一正文
    scan()
    entries = rows(pg, "SELECT * FROM index_publications")
    assert len(entries) == 1
    client = SearchClient(pg)
    client.hit(entries[0], fixed_text(pg, second)[:30])
    result = search(client)
    item = result["results"][0]
    raw = {r["version_id"]: r["raw"] for r in rows(pg, "SELECT version_id,payload->>'raw_sha256' AS raw FROM history_versions")}
    assert item["version_id"] == second and item["raw_sha256"] == raw[second]
    assert item["indexed_as"]["version_id"] == first
    assert item["artifact"]["artifact_key"] == entries[0]["artifact_key"]


def test_retrieval_masks_withdrawal_revocation_and_unproven_history_immediately(pg, monkeypatch):
    set_old_targets(monkeypatch)
    active = add_collected()
    scan()
    client = SearchClient(pg)
    client.hit(active, fixed_text(pg, active)[:20])
    assert [r["version_id"] for r in search(client)["results"]] == [active]
    add_collected(withdrawn=True)
    result = search(client)
    assert result["results"] == [] and result["excluded"]["counts"] == {"withdrawn": 1}
    # 没有外部可得证明的来源，历史重建模式不返回
    past = search(client, "public_history_reconstruction")
    assert past["excluded"]["counts"] == {"mode_unsupported_for_source": 1}
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id='legacy_documents'"))
    revoked = search(client)
    assert revoked["excluded"] == {"counts": {"not_readable": 1}, "items": []}  # 无权材料只给计数
    assert "Synthetic" not in json.dumps(revoked)


def test_crowded_candidates_report_insufficient_results_and_unlocatable_chunks(pg):
    version = add_document("SYNTHETIC-crowd", available="2019-01-01")
    scan()
    client = SearchClient(pg)
    text_ = fixed_text(pg, version)
    for _ in range(5):
        client.hit(version, text_[:25])  # 同一段：去重后一条
    client.hit(version, "this sentence is not in the fixed text")
    result = search(client, top_k=5)
    assert len(result["results"]) == 1 and result["execution"]["insufficient"]
    assert result["excluded"]["counts"] == {"chunk_not_in_fixed_text": 1}
    assert result["results"][0]["locators"] or result["results"][0]["locator_kind"] is not None
    assert "this sentence" not in " ".join(client.reranked)
    assert result["coverage"]["external_archive"]["entries_by_state"] == {"queued": 1}


def test_literal_search_over_a_huge_source_returns_query_budget(pg, monkeypatch):
    add_document("SYNTHETIC-literal")
    with pg.begin() as conn:
        conn.execute(text("UPDATE history_sources SET published_seq=published_seq+30000 WHERE source_id='external_archive'"))
    with pytest.raises(HistoryError) as error:
        history.list_records("external_archive", True, "current", datetime.now(timezone.utc), terms=["SYNTHETIC"])
    assert error.value.code == "query_budget" and "/api/history/search" in error.value.message
    # 给了记录编号的精确读取不受影响
    history.list_records("external_archive", True, "current", datetime.now(timezone.utc),
                         source_record_ids=["SYNTHETIC-literal"], terms=["SYNTHETIC"])


def test_search_stays_available_until_the_replacement_is_ready(pg, monkeypatch):
    """Codex 441：处理器交接期，检索选用的产物与 worker 的交接状态一致——新产物的条目 ready 之前，
    仍在服务的旧条目照样能检索到；新条目 ready 之后只认新产物。"""
    from processing import clean_text

    version, journal = ready_job(pg)
    old = job(pg)
    body = fixed_text(pg, version)
    client = SearchClient(pg)
    client.hit(old, body[:40])
    assert [r["artifact"]["handover"] for r in search(client)["results"]] == [False]
    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "synthetic.clean_text.v99")
    profile()
    new = next(r for r in rows(pg, "SELECT * FROM index_publications") if r["id"] != old["id"])
    due_job(pg, old["id"])
    index.process_once(client=journal)  # 旧条目 retiring：保留、不联网
    during = search(client)
    assert [(r["version_id"], r["artifact"]["handover"]) for r in during["results"]] == [(version, True)]
    assert during["results"][0]["artifact"]["artifact_key"] == old["artifact_key"]
    due_job(pg, new["id"])
    assert index.process_once(client=journal)["state"] == "ready"
    after = search(client)
    assert after["results"] == [] and after["excluded"]["counts"] == {"changed_at_cutoff": 1}


def test_parallel_equal_text_versions_create_one_physical_entry(pg, monkeypatch):
    """Codex S36-01：两个事务同时入队同一记录的同一份正文，只落一个物理条目（数据库唯一索引保证）。"""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("DATAFABRIC_INDEX_TARGETS", "[]")
    first = add_document("SYNTHETIC-parallel-same", available="2010-01-01", phase="PHASE1")
    second = add_document("SYNTHETIC-parallel-same", available="2012-01-01", phase="PHASE2")
    profile()
    assert rows(pg, "SELECT * FROM index_publications") == []
    set_targets(monkeypatch)
    with pg.begin() as conn:
        index._sync_targets(conn, index.targets())
        artifacts = index.current_artifacts(conn, [first, second])
    assert index.artifact_key(artifacts[first]) == index.artifact_key(artifacts[second])
    barrier = threading.Barrier(2)

    def enqueue(version):
        with pg.begin() as conn:
            barrier.wait(timeout=10)
            count = index.enqueue_versions(conn, [version])
            time.sleep(1)  # 事务晚一点提交，让另一个先撞上
            return count

    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(enqueue, [first, second]))
    stored = rows(pg, "SELECT version_id FROM index_publications")
    assert sorted(counts) == [0, 1] and len(stored) == 1 and stored[0]["version_id"] in {first, second}



# —— 长产物分片（Codex 448/451）——

def small_pieces(monkeypatch, chars=200):
    from processing import index_pieces

    monkeypatch.setattr(index_pieces, "MAX_CHARS", chars + index_pieces.UPLOAD_OVERHEAD)
    monkeypatch.setattr(index_pieces, "MAX_BYTES", 10 ** 9)


def long_document(native="SYNTHETIC-long"):
    title = " ".join("Synthetic long registry wording %02d about an investigational product." % i for i in range(12))
    return add_document(native, title=title)


def test_a_long_artifact_is_indexed_in_pieces_and_is_searchable_only_when_every_piece_is_ready(pg, monkeypatch):
    from processing import index_pieces

    small_pieces(monkeypatch)
    version = long_document()
    scan()
    entries = rows(pg, "SELECT * FROM index_publications ORDER BY piece_index")
    full = fixed_text(pg, version)
    assert len(entries) >= 3 and [e["piece_index"] for e in entries] == list(range(1, len(entries) + 1))
    assert len({e["artifact_key"] for e in entries}) == 1 and len({e["remote_id"] for e in entries}) == len(entries)
    assert entries[0]["piece"]["start"] == 0 and entries[-1]["piece"]["end"] == len(full)
    client = SearchClient(pg)
    assert index.process_once(client=client)["state"] == "ready"
    cover = retrieval._coverage({"external_archive"})["external_archive"]
    assert cover["partial_artifacts"] == 1 and cover["ready_artifacts"] == 0  # 只有一片 ready：不算整篇可检索
    for _ in range(len(entries)):
        index.process_once(client=client)
    assert {r["state"] for r in rows(pg, "SELECT state FROM index_publications")} == {"ready"}
    for entry, body in zip(entries, client.bodies):
        assert body["piece"] == entry["piece"] and body["content"] == index_pieces.content(full, entry["piece"])
    cover = retrieval._coverage({"external_archive"})["external_archive"]
    assert cover["ready_artifacts"] == 1 and cover["partial_artifacts"] == 0
    # 命中第 2 片：换算回全篇偏移，读的是可见版本这份产物里的原文
    second = rows(pg, "SELECT * FROM index_publications WHERE piece_index=2")[0]
    piece_text = full[second["piece"]["start"]:second["piece"]["end"]]
    client.hit(second, piece_text[5:60])
    item = search(client)["results"][0]
    assert item["start"] == second["piece"]["start"] + 5 and full[item["start"]:item["end"]] == item["text"]
    assert item["indexed_as"]["piece"]["index"] == 2


def test_handover_waits_for_every_piece_of_the_new_artifact(pg, monkeypatch):
    from processing import clean_text

    small_pieces(monkeypatch, chars=100000)
    long_document()
    scan()
    client = SearchClient(pg)
    assert index.process_once(client=client)["state"] == "ready"
    old = job(pg)
    # 处理器升级，同时分片上限变小：新产物要分几片进索引
    small_pieces(monkeypatch)
    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "synthetic.clean_text.v99")
    profile()
    new = [r for r in rows(pg, "SELECT * FROM index_publications WHERE id<>:i ORDER BY piece_index", {"i": old["id"]})]
    assert len(new) >= 2
    due_job(pg, new[0]["id"])
    assert index.process_once(client=client)["state"] == "ready"  # 新产物只 ready 了一片
    due_job(pg, old["id"])
    index.process_once(client=client)
    assert rows(pg, "SELECT state FROM index_publications WHERE id=:i", {"i": old["id"]})[0]["state"] == "ready"
    assert client.disable_count == 0  # 新产物没有整份 ready：旧条目保留
    for entry in new[1:]:
        due_job(pg, entry["id"])
        index.process_once(client=client)
    due_job(pg, old["id"])
    assert index.process_once(client=client)["desired_state"] == "superseded"
    assert client.disable_count == 1


def test_over_budget_whole_entries_are_replaced_and_chunk_budget_is_blocked_not_retried_forever(pg, monkeypatch):
    from processing import index_pieces

    version, client = ready_job(pg)
    monkeypatch.setattr(index_pieces, "MAX_CHARS", 50)  # 分片之前冻结的整篇清单，现在超过单条上传预算
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET state='queued',first_send_at=NULL,knowledge_id=NULL,"
                          "receipt='{}'::jsonb"))
    due(pg)
    plain = JournalClient()
    result = index.process_once(client=plain)
    # 没发出过：由当前分片计划替代，直接本地停用，不上传、不联网，也不无休止 indexing
    assert result["state"] == "disabled" and result["desired_state"] == "superseded" and plain.events == []
    small_pieces(monkeypatch, chars=100000)

    class TooManyChunks(JournalClient):
        def publish(self, body):
            receipt = super().publish(body)
            receipt.update(index_status="budget_exceeded", vector_status="budget_exceeded", chunk_count=2400)
            return receipt

    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET state='queued',attempts=0,error=NULL"))
    due(pg)
    result = index.process_once(client=TooManyChunks())
    assert result["state"] == "blocked" and job(pg)["error"]["code"] == "index_chunk_budget"


def test_a_whole_entry_built_before_pieces_is_replaced_by_the_current_piece_plan(pg, monkeypatch):
    """整篇条目建于分片之前（或分片规则换了）：按当前计划建的片替代它，产物在新片全部 ready 后才算可检索。"""
    small_pieces(monkeypatch, chars=100000)
    version = long_document()
    scan()
    client = SearchClient(pg)
    assert index.process_once(client=client)["state"] == "ready"
    whole = job(pg)
    small_pieces(monkeypatch)  # 现在这份产物要分片
    with pg.begin() as conn:
        index.enqueue_versions(conn, [version])
    pieces = rows(pg, "SELECT * FROM index_publications WHERE piece_index>0 ORDER BY piece_index")
    assert len(pieces) >= 3 and {p["artifact_key"] for p in pieces} == {whole["artifact_key"]}
    due_job(pg, whole["id"])
    index.process_once(client=client)  # 整篇已 ready：新片还没全 ready，保留（retiring），不撤掉入口
    assert rows(pg, "SELECT state FROM index_publications WHERE id=:i", {"i": whole["id"]})[0]["state"] == "ready"
    assert client.disable_count == 0
    for _ in range(60):
        states = {r["piece_index"]: r["state"] for r in rows(pg, "SELECT piece_index,state FROM index_publications")}
        if states[0] == "disabled" and all(states[p["piece_index"]] == "ready" for p in pieces):
            break
        if index.process_once(client=client)["state"] == "idle":
            due(pg)  # 都处理过一轮了，再让它们到期复查
    assert states[0] == "disabled" and all(states[p["piece_index"]] == "ready" for p in pieces)
    assert client.disable_count == 1
    cover = retrieval._coverage({"external_archive"})["external_archive"]
    assert cover["ready_artifacts"] == 1 and cover["partial_artifacts"] == 0


def test_a_new_piece_rule_adds_new_pieces_beside_the_old_ones_and_retires_them_after(pg, monkeypatch):
    """Codex 456：唯一键和编号带完整分片身份（规则、序号、范围、片摘要）：分片规则换了，同序号的新片不会被
    旧片挡住（ON CONFLICT 悄悄跳过）；旧片在新片全部 ready 后才停用。"""
    from processing import index_pieces

    small_pieces(monkeypatch)
    version = long_document()
    scan()
    client = SearchClient(pg)
    old = rows(pg, "SELECT * FROM index_publications ORDER BY piece_index")
    while index.process_once(client=client)["state"] != "idle":
        pass
    assert {r["state"] for r in rows(pg, "SELECT state FROM index_publications")} == {"ready"}
    monkeypatch.setattr(index_pieces, "RULE", "datafabric.index_piece.v2-synthetic")
    with pg.begin() as conn:
        index.enqueue_versions(conn, [version])
    new = rows(pg, "SELECT * FROM index_publications WHERE piece->>'rule'=:r ORDER BY piece_index",
               {"r": "datafabric.index_piece.v2-synthetic"})
    assert len(new) == len(old) and {r["piece_index"] for r in new} == {r["piece_index"] for r in old}
    assert not {r["id"] for r in new} & {r["id"] for r in old}
    assert not {r["remote_id"] for r in new} & {r["remote_id"] for r in old}
    for _ in range(80):
        states = {r["id"]: r["state"] for r in rows(pg, "SELECT id,state FROM index_publications")}
        if all(states[r["id"]] == "disabled" for r in old) and all(states[r["id"]] == "ready" for r in new):
            break
        if index.process_once(client=client)["state"] == "idle":
            due(pg)
    assert all(states[r["id"]] == "disabled" for r in old) and all(states[r["id"]] == "ready" for r in new)
    assert client.disable_count == len(old)


def test_old_pieces_stay_until_every_piece_of_the_new_plan_is_ready(pg, monkeypatch):
    """Codex 460 的反例：旧计划 3 片都 ready，新计划要 5 片。新计划的每一片都要建出来（不被旧片的序号挡住），
    数据库里只有其中几片 ready 时旧片照样保留、检索照样认旧片；整套新片 ready 后旧片才停用。"""
    from processing import index_pieces

    small_pieces(monkeypatch)
    version = long_document()
    scan()
    client = SearchClient(pg)
    while index.process_once(client=client)["state"] != "idle":
        pass
    old = rows(pg, "SELECT * FROM index_publications ORDER BY piece_index")
    small_pieces(monkeypatch, chars=120)  # 同一规则、同一产物，切点变了，片数更多
    with pg.begin() as conn:
        index.enqueue_versions(conn, [version])
    new = rows(pg, "SELECT * FROM index_publications WHERE NOT id=ANY(:old) ORDER BY piece_index",
               {"old": [r["id"] for r in old]})
    with pg.begin() as conn:
        artifact = index.current_artifacts(conn, [version])[version]
    assert len(new) > len(old) >= 3 and [r["piece_index"] for r in new] == list(range(1, len(new) + 1))
    assert index_pieces.keys(artifact["text"], artifact["segments"]) == {r["piece_key"] for r in new}

    def run_only(ids, until):
        """只让 ids 到期，处理到这些条目都到 until 状态（或已处理一轮而状态不再变）。"""
        for _ in range(20):
            with pg.begin() as conn:
                conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()+CASE WHEN "
                                  "id=ANY(:ids) THEN interval '0' ELSE interval '1 day' END"), {"ids": ids})
            while index.process_once(client=client)["state"] != "idle":
                pass
            states = {r["id"]: r["state"] for r in rows(pg, "SELECT id,state FROM index_publications")}
            if all(states[i] == until for i in ids):
                return states
        return states

    run_only([r["id"] for r in new if r["piece_index"] > len(old)], "ready")  # 只有序号在旧片数之后的几片 ready
    states = run_only([r["id"] for r in old], "disabled")  # 旧片复查：新计划还没整套 ready，必须保留
    assert all(states[r["id"]] == "ready" for r in old) and client.disable_count == 0
    with pg.begin() as conn:
        assert retrieval._serving_old(conn, old[0]["target_id"], old[0]["source_record_id"], artifact,
                                      old[0]["artifact_key"], old[0]["piece_key"])
    cover = retrieval._coverage({"external_archive"})["external_archive"]
    assert cover["ready_artifacts"] == 1 and cover["partial_artifacts"] == 0  # 旧计划整套 ready，覆盖全篇
    old_ids = [r["id"] for r in old]
    with pg.begin() as conn:  # 假设旧片不在：只有新计划的后几片 ready，覆盖不到全篇，不算整份可检索
        conn.execute(text("UPDATE index_publications SET state='waiting' WHERE id=ANY(:ids)"), {"ids": old_ids})
    cover = retrieval._coverage({"external_archive"})["external_archive"]
    assert cover["ready_artifacts"] == 0 and cover["partial_artifacts"] == 1
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET state='ready' WHERE id=ANY(:ids)"), {"ids": old_ids})
    run_only([r["id"] for r in new], "ready")
    states = run_only([r["id"] for r in old], "disabled")
    assert all(states[r["id"]] == "ready" for r in new) and all(states[r["id"]] == "disabled" for r in old)
    assert client.disable_count == len(old)
    cover = retrieval._coverage({"external_archive"})["external_archive"]
    assert cover["ready_artifacts"] == 1 and cover["partial_artifacts"] == 0  # 新计划整套 ready
    with pg.begin() as conn:
        assert not retrieval._serving_old(conn, old[0]["target_id"], old[0]["source_record_id"], artifact,
                                          old[0]["artifact_key"], old[0]["piece_key"])


def rerun_file(engine, name):
    """Run one migration file on a store that was built before it (a file is idempotent; the runner runs it once, on
    a ledger that has not recorded it, and this is that run)."""
    with engine.begin() as conn:
        conn.exec_driver_sql((Path(index_migration.__file__).parent / "versions" / name).read_text(encoding="utf-8"))


def test_a_store_built_with_ordinal_piece_keys_upgrades_in_place_and_reruns(pg, monkeypatch):
    """Codex r83：piece_key 之前的分片库（按分片序号的两个唯一键、没有 piece_key 列）跑索引迁移：从已有 piece
    回填完整身份键，去掉旧唯一键、建新唯一键；id、remote_id、清单、回执都不变；再跑一次什么也不变。"""
    from processing import index_pieces

    small_pieces(monkeypatch)
    long_document()
    scan()
    client = SearchClient(pg)
    while index.process_once(client=client)["state"] != "idle":
        pass
    query = "SELECT id,remote_id,manifest,receipt,state,piece,piece_key FROM index_publications ORDER BY id"
    before = rows(pg, query)
    assert len(before) >= 3 and all(r["piece_key"] == index_pieces.key(r["piece"]) for r in before)
    with pg.begin() as conn:  # 回到 2b4011f 时的表
        conn.execute(text("DROP INDEX index_publications_artifact_piece_key"))
        conn.execute(text("DROP INDEX index_publications_record_artifact_piece_key"))
        conn.execute(text("ALTER TABLE index_publications DROP COLUMN piece_key"))
        conn.execute(text("CREATE UNIQUE INDEX index_publications_artifact_piece ON index_publications "
                          "(target_id, version_id, artifact_key, piece_index)"))
        conn.execute(text("CREATE UNIQUE INDEX index_publications_record_artifact_piece ON index_publications "
                          "(target_id, source_record_id, artifact_key, piece_index) WHERE artifact_key <> ''"))
    for _ in range(2):
        rerun_file(pg, "V028__index_worker.sql")  # an older store has no record of V028: its file runs on it
        assert rows(pg, query) == before
        names = {r["indexname"] for r in rows(pg, "SELECT indexname FROM pg_indexes WHERE "
                                                  "schemaname=current_schema() AND tablename='index_publications'")}
        assert {"index_publications_artifact_piece_key", "index_publications_record_artifact_piece_key"} <= names
        assert not names & {"index_publications_artifact_piece", "index_publications_record_artifact_piece"}


def test_ready_pieces_that_do_not_reach_the_verified_end_are_partial_coverage(pg, monkeypatch):
    """Codex r84：只剩首片且 ready 时，现存片的最大终点不是全文长度；全篇长只取末片（序号 = 片数）的终点，
    没有末片就是部分覆盖。首片加末片都 ready 而中间缺：仍是部分；整套齐了才是整份。"""
    small_pieces(monkeypatch, chars=400)
    long_document("SYNTHETIC-missing-tail")
    scan()
    pieces = rows(pg, "SELECT * FROM index_publications ORDER BY piece_index")
    assert len(pieces) >= 3
    client = SearchClient(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()+interval '1 day'"))
    due_job(pg, pieces[0]["id"])
    assert index.process_once(client=client)["state"] == "ready"
    cover = lambda: retrieval._coverage({"external_archive"})["external_archive"]  # noqa: E731
    with pg.begin() as conn:  # 只剩首片
        kept = conn.execute(text("SELECT * FROM index_publications WHERE id<>:id"), {"id": pieces[0]["id"]})
        others = [dict(r) for r in kept.mappings()]
        conn.execute(text("UPDATE index_publications SET state='disabled' WHERE id<>:id"), {"id": pieces[0]["id"]})
    got = cover()
    assert got["ready_artifacts"] == 0 and got["partial_artifacts"] == 1
    with pg.begin() as conn:  # 首片和末片 ready，中间缺
        conn.execute(text("UPDATE index_publications SET state='ready' WHERE id=:id"), {"id": pieces[-1]["id"]})
    got = cover()
    assert got["ready_artifacts"] == 0 and got["partial_artifacts"] == 1
    with pg.begin() as conn:  # 整套 ready
        conn.execute(text("UPDATE index_publications SET state='ready' WHERE id=ANY(:ids)"),
                     {"ids": [r["id"] for r in others]})
    got = cover()
    assert got["ready_artifacts"] == 1 and got["partial_artifacts"] == 0
    with pg.begin() as conn:  # 只有首片这一行（其余行都不在）：末片不在，全篇长未知
        conn.execute(text("DELETE FROM index_publications WHERE id<>:id"), {"id": pieces[0]["id"]})
    got = cover()
    assert got["ready_artifacts"] == 0 and got["partial_artifacts"] == 1


def test_a_job_that_overruns_its_lease_goes_behind_the_others_and_the_lease_covers_the_slowest_call(pg):
    """Isolated run: a slow disable (up to 180 s) under a 120 s lease lost its lease every time, kept its old
    next_check_at and was claimed first again, so the jobs behind it never ran. The lease now outlasts the
    slowest bridge call, and claiming a job moves its next check past the lease."""
    import inspect

    from core_engine import weknora_index_client

    assert "seconds=180" in inspect.getsource(weknora_index_client.WeKnoraIndexClient.disable)
    assert index.LEASE_SECONDS > 180 + 20
    first = add_document("SYNTHETIC-lease-a")
    second = add_document("SYNTHETIC-lease-b")
    scan()
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()-interval '1 hour' "
                          "WHERE version_id=:v"), {"v": first})
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()-interval '1 minute' "
                          "WHERE version_id=:v"), {"v": second})
    claimed = index._claim()
    assert claimed["version_id"] == first
    with pg.begin() as conn:  # the worker died or overran: its lease is gone, the result never recorded
        conn.execute(text("UPDATE index_publications SET lease_until=clock_timestamp()-interval '1 second' "
                          "WHERE id=:i"), {"i": claimed["id"]})
    assert index._claim()["version_id"] == second  # not the same job again at the head of the queue


def test_a_profile_upgrade_before_reprofiling_disables_nothing(pg, monkeypatch):
    """v3 6.2 bumped the profile version: until versions are profiled again no artifact is current, and a sent
    entry must then be kept (retiring), not disabled because "every replacement is ready" held vacuously."""
    from processing import profile as profile_module

    version, client = ready_job(pg)
    monkeypatch.setattr(profile_module, "PROCESSOR_VERSION", "datafabric.profile.v999")
    due(pg)
    index.process_once(client=client)
    saved = job(pg)
    assert saved["state"] == "ready" and saved["desired_state"] != "superseded"  # kept, nothing sent to disable


def test_a_retired_source_is_not_recalled_is_named_in_the_receipt_and_a_mixed_query_keeps_the_others(pg, monkeypatch):
    """With legacy_documents retired semantic search does not choose it: not by default, not when asked for it alone (410),
    and a mixed query searches only the other sources and says which one it left out. No environment setting changes it."""
    legacy_bridge.close_old_writer(monkeypatch)
    set_targets(monkeypatch)  # external_archive
    client = SearchClient(pg)
    asked = []
    original = client.search
    client.search = lambda body: asked.append(body["knowledge_base_id"]) or original(body)
    assert search(client)["excluded_sources"] == []
    mixed = search(client, sources=("external_archive", "legacy_documents"))
    assert mixed["excluded_sources"] == [{"source_id": "legacy_documents", "reason": "retired"}]
    assert set(asked) == {"synthetic-isolated-kb"}
    for setting in ("off", "on"):
        monkeypatch.setenv("DATAFABRIC_LEGACY_WRITES", setting)  # the old cutover switch is read by nothing
        with pytest.raises(HistoryError) as error:
            search(client, sources=("legacy_documents",))
        assert (error.value.code, error.value.status) == ("legacy_retired", 410)
        assert search(client, sources=("external_archive", "legacy_documents"))["excluded_sources"] == [
            {"source_id": "legacy_documents", "reason": "retired"}]


def test_an_index_target_naming_a_retired_source_is_a_configuration_error_found_at_start(pg, monkeypatch):
    """N4: refused by the loader, so by every caller - search, the worker, and the service's startup check."""
    monkeypatch.setenv("DATAFABRIC_INDEX_TARGETS", canonical([
        {"source_instance": "synthetic.index.pg", "source_id": "external_archive", "is_demo": True,
         "knowledge_base_id": "synthetic-isolated-kb", "source_scope": "research"},
        {"source_instance": "synthetic.index.pg", "source_id": "legacy_documents", "is_demo": True,
         "knowledge_base_id": "synthetic-legacy-kb", "source_scope": "research"}]))
    from api import main

    for call in (index.targets, lambda: search(SearchClient(pg)), lambda: index.run_worker(rounds=0),
                 main._validate_source_configuration):
        with pytest.raises(HistoryError) as error:
            call()
        assert (error.value.code, error.value.status) == ("index_target_retired_source", 503)
        assert "legacy_documents" in error.value.message
    set_targets(monkeypatch)
    assert [t.source_id for t in index.targets()] == ["external_archive"]
    main._validate_source_configuration()


def test_a_new_artifact_still_parsing_or_failed_never_replaces_the_old_entry(pg, monkeypatch):
    """T-26（Codex 708）：同一 raw version 升级清洗处理器。新产物在 WeKnora 还在解析或解析失败时，旧条目照常服务
    （handover），新条目不算可检索；新产物 ready 后旧条目才停用，旧条目的知识编号和回执保留，旧命中不再采用。"""
    from processing import clean_text

    version, journal = ready_job(pg)
    old = job(pg)
    body = fixed_text(pg, version)
    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "synthetic.clean_text.v99")
    profile()
    new = next(r for r in rows(pg, "SELECT * FROM index_publications") if r["id"] != old["id"])
    assert new["version_id"] == old["version_id"] == version and new["artifact_key"] != old["artifact_key"]

    class Remote(SearchClient):
        parse = "processing"  # what WeKnora says of the new artifact's entry

        def publish(self, body):
            super().publish(body)
            return self.status(publication_id(body))

        def status(self, pid):
            got = super().status(pid)
            if got is not None and pid == new["remote_id"] and self.parse != "completed":
                failed = self.parse == "failed"
                got.update(parse_status=self.parse, index_status="failed" if failed else "indexing",
                           vector_status="failed" if failed else "pending", chunk_count=0, vector_count=0)
            return got

    client = Remote(pg)
    client.journal.update(copy.deepcopy(journal.journal))
    client.hit(old, body[:40])

    def serving():
        found = search(client)["results"]
        return [(r["version_id"], r["artifact"]["artifact_key"], r["artifact"]["handover"]) for r in found]

    seen = []
    for parse in ("processing", "failed"):
        client.parse = parse
        due_job(pg, new["id"])
        seen.append(index.process_once(client=client)["state"])
        due_job(pg, old["id"])
        index.process_once(client=client)  # the old entry: retiring, kept, no network
        assert job_state(pg, old["id"]) == ("ready", "active") and job_state(pg, new["id"])[0] != "ready"
        seen.append(job_state(pg, new["id"])[0])
        assert serving() == [(version, old["artifact_key"], True)]  # still the old artifact, said so
    assert "ready" not in seen and client.disable_count == 0
    client.parse = "completed"
    with pg.begin() as conn:  # WeKnora parsed it on a retry
        conn.execute(text("UPDATE index_publications SET attempts=0 WHERE id=:i"), {"i": new["id"]})
    due_job(pg, new["id"])
    assert index.process_once(client=client)["state"] == "ready"
    due_job(pg, old["id"])
    assert index.process_once(client=client)["desired_state"] == "superseded"
    kept = rows(pg, "SELECT state, knowledge_id, receipt FROM index_publications WHERE id=:i", {"i": old["id"]})[0]
    assert kept["state"] == "disabled" and kept["knowledge_id"] == old["knowledge_id"] and kept["receipt"]
    after = search(client)
    assert after["results"] == [] and after["excluded"]["counts"] == {"changed_at_cutoff": 1}


def job_state(pg, job_id):
    got = rows(pg, "SELECT state, desired_state FROM index_publications WHERE id=:i", {"i": job_id})[0]
    return got["state"], got["desired_state"]
