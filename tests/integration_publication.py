"""真实两schema PostgreSQL：实际原库写入触发待办再自动发表，不用SQLite或HTTP假写库。"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from test_publication import snapshot

from biz_rules.schemas import ConfidenceInfo, MetadataEnvelope, ProvenanceInfo
from db import history, publication
from db.models import Base, Document, EntityDict, EntityRelation, ExtractedEntity
from migration import history as history_migration
from migration import publication as publication_migration


@pytest.fixture
def databases(monkeypatch):
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL required")
    parsed = make_url(url)
    assert (
        parsed.host == "127.0.0.1"
        and (parsed.port, parsed.database) in {(18436, "fabric_history"), (18636, "bioradar_kdd_opus_history")}
    )
    admin = create_engine(url)
    names = ["test_publication_" + uuid4().hex, "test_destination_" + uuid4().hex]
    with admin.begin() as conn:
        for name in names:
            conn.execute(text("CREATE SCHEMA " + name))
    engines = [
        create_engine(
            url, connect_args={"options": "-csearch_path=" + name + (",public" if index == 0 else "")}
        )
        for index, name in enumerate(names)
    ]
    source, target = engines
    Base.metadata.create_all(source)
    monkeypatch.setattr(publication, "publication_engine", lambda: source)
    monkeypatch.setattr(publication_migration, "publication_engine", lambda: source)
    monkeypatch.setattr(history, "history_engine", lambda: target)
    monkeypatch.setattr(history_migration, "history_engine", lambda: target)
    monkeypatch.setenv(
        "DATAFABRIC_HISTORY_SOURCE_ALLOWLIST",
        '["clinicaltrials","legacy_documents","reviewed_graph","external_archive"]',
    )
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_SOURCES", '["clinicaltrials"]')
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_IS_DEMO", "true")
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_NAMESPACE", "synthetic.publication")
    history_migration.migrate_history()
    publication_migration.migrate_publication()
    import db.session

    @contextmanager
    def sessions():
        with Session(source) as session, session.begin():
            yield session

    monkeypatch.setattr(db.session, "get_session", sessions)
    try:
        yield source, target
    finally:
        for engine in engines:
            engine.dispose()
        with admin.begin() as conn:
            for name in names:
                conn.execute(text("DROP SCHEMA " + name + " CASCADE"))
        admin.dispose()


def insert_document(engine, value):
    with Session(engine) as session, session.begin():
        row = Document(**value)
        session.add(row)
        session.flush()
        return row.id


def rows(engine, sql):
    with engine.begin() as conn:
        return list(conn.execute(text(sql)).mappings())


def version_count(target):
    return rows(target, "SELECT count(*) n FROM history_versions")[0]["n"]


def test_source_transaction_rollback_and_real_commit_gate_publication(databases):
    source, target = databases
    with pytest.raises(RuntimeError), Session(source) as session, session.begin():
        session.add(Document(**snapshot("rollback")))
        session.flush()
        assert (
            session.execute(
                text("SELECT count(*) FROM publication_outbox")
            ).scalar_one()
            == 1
        )
        raise RuntimeError("explicit rollback")
    assert not rows(source, "SELECT * FROM publication_outbox")
    insert_document(source, snapshot("committed"))
    result = publication.publish_once()
    assert result["state"] == "published" and version_count(target) == 1
    saved = rows(source, "SELECT snapshot,prepared,receipt FROM publication_outbox")[0]
    assert saved["prepared"] is None and "raw_content" not in json.dumps(
        saved["snapshot"]
    )
    assert saved["receipt"]["document_version_ids"]
    assert publication.publish_once()["state"] == "idle"


def test_commit_unknown_reuses_same_frozen_input_and_key(databases, monkeypatch):
    source, target = databases
    insert_document(source, snapshot())
    original = history.import_archive

    def commit_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic ACK connection loss after durable history commit")

    monkeypatch.setattr(history, "import_archive", commit_then_fail)
    assert publication.publish_once()["state"] == "failed"
    assert version_count(target) == 1
    frozen = rows(source, "SELECT prepared FROM publication_outbox")[0]["prepared"]
    with source.begin() as conn:
        conn.execute(
            text("UPDATE publication_outbox SET next_attempt_at=clock_timestamp()")
        )
    monkeypatch.setattr(history, "import_archive", original)
    assert publication.publish_once()["state"] == "published"
    assert version_count(target) == 1
    assert rows(target, "SELECT count(*) n FROM history_runs")[0]["n"] == 1
    assert frozen["document_request"]["records"][0]["raw_content"]


def test_new_body_is_durable_without_overwriting_old_graph_cache(databases):
    from db.repository import upsert_document

    source, target = databases
    value = snapshot()
    insert_document(source, value)
    assert publication.publish_once()["state"] == "published"
    changed = snapshot(status="TERMINATED")
    envelope = MetadataEnvelope(
        uuid=value["uuid"],
        title=value["title"],
        raw_content=changed["raw_content"],
        provenance=ProvenanceInfo(
            source_site="clinicaltrials", source_url=value["source_url"]
        ),
        confidence=ConfidenceInfo(score=1, reason="SYNTHETIC"),
    )
    result = upsert_document(envelope)
    assert result["legacy_cache_retained"] and result["publication_pending"]
    assert (
        rows(source, "SELECT raw_content FROM documents")[0]["raw_content"]
        == value["raw_content"]
    )
    assert publication.publish_once()["state"] == "published"
    assert version_count(target) == 2
    items = history.list_changes("legacy_documents", True)["items"]
    assert [item["facts"]["status"] for item in items] == ["RECRUITING", "TERMINATED"]


def test_unimportant_updates_do_not_emit_text_and_delete_is_explicit(databases):
    source, _target = databases
    insert_document(source, snapshot())
    assert publication.publish_once()["state"] == "published"
    with source.begin() as conn:
        conn.execute(
            text(
                "UPDATE documents SET extraction_status='done',updated_at=clock_timestamp()"
            )
        )
    assert publication.publish_once()["state"] == "idle"
    with source.begin() as conn:
        conn.execute(text("UPDATE documents SET is_deleted=true"))
    assert publication.publish_once()["state"] == "published"
    items = history.list_changes("legacy_documents", True)["items"]
    assert items[-1]["facts"]["publication_state"] == "withdrawn"
    assert (
        items[-1]["raw_content"]
        and not json.loads(items[-1]["raw_content"])["original_raw_content"]
    )
    assert items[0][
        "raw_content"
    ]  # Old immutable record remains, with current source permission.


def test_failed_head_and_expired_lease_do_not_skip_newer_versions(databases):
    source, target = databases
    insert_document(source, snapshot())
    with source.begin() as conn:
        conn.execute(text("UPDATE documents SET raw_content=raw_content || ' revised'"))
    first = publication._claim(source)
    assert first is not None and publication._claim(source) is None
    with source.begin() as conn:
        conn.execute(
            text(
                "UPDATE publication_outbox SET lease_until=clock_timestamp()-interval '1 second'"
            )
        )
    assert publication.publish_once()["state"] == "published"
    assert publication.publish_once()["state"] == "published"
    assert version_count(target) == 2


def test_bounded_backfill_is_resumable_and_does_not_label_inventory_as_new(databases):
    source, target = databases
    for i in range(4):
        insert_document(source, snapshot("backfill-" + str(i)))
    # Existing stock before trigger installation is emulated by removing these pending rows.
    with source.begin() as conn:
        conn.execute(text("DELETE FROM publication_outbox"))
    first = publication.backfill_once(limit=2)
    assert first["seeded"] == 2 and not first["complete"]
    assert publication.publish_once()["state"] == "published"
    waiting = publication.backfill_once(limit=2)
    assert waiting["state"] == "pending_first" and waiting["seeded"] == 0
    assert publication.publish_once()["state"] == "published"
    second = publication.backfill_once(limit=2)
    assert second["seeded"] == 2 and second["complete"]
    for _ in range(2):
        assert publication.publish_once()["state"] == "published"
    assert publication.backfill_once(limit=2)["seeded"] == 0
    assert version_count(target) == 4
    assert all(
        item["facts"]["capture_reason"] == "backfill"
        for item in history.list_changes("legacy_documents", True)["items"]
    )


def test_unknown_source_and_disk_pause_are_visible_and_preserve_pending(
    databases, monkeypatch
):
    source, target = databases
    value = snapshot()
    value["source_site"] = "unknown"
    insert_document(source, value)
    assert publication.publish_once()["state"] == "blocked"
    assert version_count(target) == 0
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_MIN_FREE_BYTES", str(2**62))
    assert publication.backfill_once()["state"] == "paused_disk_low"
    assert publication.publish_once()["state"] == "resource_blocked"
    publication.publisher_heartbeat("resource_blocked")
    status = publication.publication_status()
    assert status["publisher_state"] == "resource_blocked" and status["last_heartbeat"]
    assert status["counts"]["blocked"] == 1
    assert version_count(target) == 0


def test_graph_review_update_then_rejection_publishes_new_bound_snapshot(databases):
    source, target = databases
    value = snapshot()
    # Quotes are in the narrative prefix, not in JSON keys.
    value["raw_content"] = (
        "---\ntitle: SYNTHETIC\nsource_site: clinicaltrials\ndoc_type: clinical_trial\nlanguage: en\n---\n\n"
        + value["raw_content"].replace(
            "SYNTHETIC narrative.", "SYNTHETIC A collaborates with B."
        )
    )
    insert_document(source, value)
    assert publication.publish_once()["state"] == "published"
    with Session(source) as session, session.begin():
        identities = [
            EntityDict(entity_type="Company", is_verified=True) for _ in range(2)
        ]
        session.add_all(identities)
        session.flush()
        entities = [
            ExtractedEntity(
                source_doc_uuid=value["uuid"],
                source_site="clinicaltrials",
                entity_type="Company",
                entity_name=name,
                review_status="approved",
                dict_id=identity.id,
                source_text="SYNTHETIC A collaborates with B.",
            )
            for name, identity in zip(("A", "B"), identities)
        ]
        session.add_all(entities)
        session.flush()
        relation = EntityRelation(
            source_doc_uuid=value["uuid"],
            from_entity_id=entities[0].id,
            to_entity_id=entities[1].id,
            relation_type="collaborated",
            review_status="approved",
            source_text="SYNTHETIC A collaborates with B.",
        )
        session.add(relation)
        session.flush()
        relation_id = relation.id
    result = publication.publish_once()
    assert result["state"] == "published"
    assert version_count(target) == 2  # Same text is reused; only graph is added.
    graph_changes = history.list_changes("reviewed_graph", True)["items"]
    graph = json.loads(graph_changes[0]["raw_content"])
    assert len(graph["edges"]) == 1 and graph["edges"][0]["binding_status"] == "bound"
    proof = graph["edges"][0]["evidence"][0]
    adopted = history.read_version(
        proof["version_id"],
        proof["source_id"],
        proof["source_record_id"],
        True,
        "current",
        datetime.now(timezone.utc),
    )
    locator = proof["locator"]
    assert locator["basis"] == "clean_text"
    assert adopted.clean_text[locator["start"] : locator["end"]] == proof["quote"]
    assert locator["start"] < value["raw_content"].index(proof["quote"])
    with source.begin() as conn:
        conn.execute(
            text("UPDATE entity_relations SET review_status='rejected' WHERE id=:id"),
            {"id": relation_id},
        )
    assert publication.publish_once()["state"] == "published"
    changed = history.list_changes("reviewed_graph", True)["items"]
    assert len(changed) == 2 and not json.loads(changed[-1]["raw_content"])["edges"]


def test_source_hard_delete_emits_empty_graph_without_reloading_missing_document(
    databases,
):
    source, target = databases
    insert_document(source, snapshot("hard-delete"))
    assert publication.publish_once()["state"] == "published"
    with source.begin() as conn:
        conn.execute(text("DELETE FROM documents"))
    assert publication.publish_once()["state"] == "published"
    result = publication.publish_once()
    assert result["state"] == "published"
    graph = history.list_changes("reviewed_graph", True)["items"][0]
    raw = json.loads(graph["raw_content"])
    assert not raw["nodes"] and not raw["edges"]
    assert "撤回" in raw["title"]
    assert version_count(target) == 3


def test_status_heartbeat_is_not_fabricated_and_revocation_is_not_idle(databases):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routers.history import router

    source, target = databases
    app = FastAPI()
    app.include_router(router)
    from unittest.mock import patch

    # Endpoint uses the same actual source/target engines, not a fake successful read.
    with (
        patch.dict(
            os.environ, {"DATAFABRIC_HISTORY_READ_TOKEN": "SYNTHETIC-read-only"}
        ),
        TestClient(app) as client,
    ):
        response = client.get(
            "/api/history/publication/status",
            headers={"Authorization": "Bearer SYNTHETIC-read-only"},
        )
        assert (
            response.status_code == 200
            and response.json()["publisher_state"] == "not_started"
        )
        publication.publisher_heartbeat("idle")
        result = client.get(
            "/api/history/publication/status",
            headers={"Authorization": "Bearer SYNTHETIC-read-only"},
        )
        assert (
            result.json()["last_heartbeat"]
            and result.json()["publisher_state"] == "idle"
        )
        with source.begin() as conn:
            conn.execute(
                text(
                    "UPDATE publication_outbox SET snapshot=jsonb_set(snapshot,'{last_heartbeat}',to_jsonb((clock_timestamp()-interval '1 hour')::text)) WHERE kind='control'"
                )
            )
        assert publication.publication_status()["publisher_state"] == "stale"
        with target.begin() as conn:
            conn.execute(
                text(
                    "UPDATE history_sources SET read_enabled=false WHERE source_id='legacy_documents'"
                )
            )
        denied = client.get(
            "/api/history/publication/status",
            headers={"Authorization": "Bearer SYNTHETIC-read-only"},
        )
        assert denied.status_code == 403
        assert "counts" not in denied.json()


def test_body_a_b_a_and_withdraw_then_restore_are_real_observations(databases):
    from db.repository import upsert_document

    source, target = databases
    value = snapshot("return-to-A")
    insert_document(source, value)
    first = publication.publish_once()

    def envelope(raw):
        return MetadataEnvelope(
            uuid=value["uuid"],
            title=value["title"],
            raw_content=raw,
            provenance=ProvenanceInfo(
                source_site="clinicaltrials", source_url=value["source_url"]
            ),
            confidence=ConfidenceInfo(score=1, reason="SYNTHETIC"),
        )

    upsert_document(envelope(snapshot(status="TERMINATED")["raw_content"]))
    second = publication.publish_once()
    upsert_document(
        envelope(value["raw_content"])
    )  # Same as old compatibility cache, newer than B.
    third = publication.publish_once()
    assert all(item["state"] == "published" for item in (first, second, third))
    assert version_count(target) == 3
    items = history.list_changes("legacy_documents", True)["items"]
    assert [item["facts"]["status"] for item in items] == [
        "RECRUITING",
        "TERMINATED",
        "RECRUITING",
    ]
    assert (
        items[0]["facts"]["original_raw_sha256"]
        == items[2]["facts"]["original_raw_sha256"]
    )
    assert (
        items[0]["version_id"] != items[2]["version_id"]
    )  # New local observation, not independent research.
    upsert_document(envelope(value["raw_content"]))
    assert (
        publication.publish_once()["state"] == "idle"
    )  # Consecutive duplicate has no new observation.
    with source.begin() as conn:
        conn.execute(text("UPDATE documents SET is_deleted=true"))
    assert publication.publish_once()["state"] == "published"
    assert publication.publish_once()["state"] == "published"  # Empty graph state.
    with source.begin() as conn:
        conn.execute(text("UPDATE documents SET is_deleted=false"))
    assert publication.publish_once()["state"] == "published"
    restored = history.list_changes("legacy_documents", True)["items"][-1]
    assert restored["facts"]["publication_state"] == "active"


def test_backfill_and_old_graph_reference_never_replace_newer_cleaned_candidate(
    databases,
):
    from db.repository import upsert_document

    source, _target = databases
    value = snapshot("compatibility-cache")
    insert_document(source, value)
    with source.begin() as conn:
        conn.execute(text("DELETE FROM publication_outbox"))
    newer = snapshot(status="TERMINATED")
    upsert_document(
        MetadataEnvelope(
            uuid=value["uuid"],
            title=value["title"],
            raw_content=newer["raw_content"],
            provenance=ProvenanceInfo(
                source_site="clinicaltrials", source_url=value["source_url"]
            ),
            confidence=ConfidenceInfo(score=1, reason="SYNTHETIC"),
        )
    )
    current = publication.publish_once()
    assert current["state"] == "published"
    assert publication.backfill_once(limit=2)["seeded"] == 0
    with Session(source) as session, session.begin():
        session.add(
            ExtractedEntity(
                source_doc_uuid=value["uuid"],
                source_site="clinicaltrials",
                entity_type="Company",
                entity_name="SYNTHETIC A",
                review_status="auto_approved",
            )
        )
    assert publication.publish_once()["state"] == "published"
    page = history.list_changes("legacy_documents", True)
    assert len(page["items"]) == 2
    assert page["items"][-1]["facts"]["publication_role"] == "context_reference"
    native = page["items"][0]["source_record_id"]
    assert page["current_version_ids"][native] == current["document_version_ids"][0]
    collection = history.list_records(
        "legacy_documents", True, "current", datetime.now(timezone.utc)
    )
    assert collection.items[0].facts["status"] == "TERMINATED"


def test_extra_write_failure_rolls_back_document_and_capture_in_same_source_transaction(
    databases, monkeypatch
):
    from db import repository

    source, target = databases
    value = snapshot("extra-rollback")

    def broken(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC extra insert failed after document flush")

    monkeypatch.setattr(repository, "_upsert_doc_extra_in_session", broken)
    envelope = MetadataEnvelope(
        uuid=value["uuid"],
        title=value["title"],
        raw_content=value["raw_content"],
        provenance=ProvenanceInfo(source_site="clinicaltrials"),
        confidence=ConfidenceInfo(score=1, reason="SYNTHETIC"),
        extra={"field": "SYNTHETIC"},
    )
    with pytest.raises(RuntimeError, match="extra insert failed"):
        repository.upsert_document(envelope)
    assert not rows(source, "SELECT * FROM documents")
    assert not rows(source, "SELECT * FROM publication_outbox")
    assert version_count(target) == 0


def test_dictionary_qualification_withdrawal_dirties_referencing_graph_only(databases, monkeypatch):
    from api.review_auth import principal_from_token
    from db import repository
    source, _target = databases
    value = snapshot("dict-qualification")
    value["raw_content"] = "SYNTHETIC company sponsors this trial.\\n" + value["raw_content"]
    insert_document(source, value)
    original = publication.publish_once()
    assert original["state"] == "published"
    with Session(source) as session, session.begin():
        identity = EntityDict(entity_type="Company", is_verified=True)
        session.add(identity); session.flush()
        identity_id = identity.id
        entity = ExtractedEntity(source_doc_uuid=value["uuid"], source_site="clinicaltrials",
            entity_type="Company", entity_name="SYNTHETIC company", source_text="SYNTHETIC company",
            review_status="approved", dict_id=identity.id)
        session.add(entity); session.flush()
        entity_id = entity.id
    assert publication.publish_once()["state"] == "published"
    first = history.list_changes("reviewed_graph", True)["items"]
    assert json.loads(first[-1]["raw_content"])["nodes"][0]["identity_status"] == "unresolved"
    # A real trusted review re-reads the actual immutable History original.
    monkeypatch.setenv("DATAFABRIC_REVIEW_TOKENS", json.dumps({"fixture-token": "synthetic-reviewer"}))
    monkeypatch.setattr(repository, "_write_aliases_with_embedding", lambda *a, **k: 0)
    record = history.read_version(original["document_version_ids"][0], "legacy_documents",
        "legacy:clinicaltrials:" + value["uuid"], True, "current", datetime.now(timezone.utc))
    quote = "SYNTHETIC company"
    start = record.clean_text.index(quote)
    evidence = {"version_id": record.version_id, "quote": quote, "char_start": start, "char_end": start+len(quote)}
    repository.approve_entity_with_alias_writeback(entity_id, "", confirm_identity=True,
        basis="Synthetic identity checked against the fixed source", evidence=[evidence],
        review_context=principal_from_token("fixture-token"))
    for _ in range(10):
        result = publication.publish_once()
        assert result["state"] in {"published", "idle"}
        if result["state"] == "idle":
            break
    confirmed = history.list_changes("reviewed_graph", True)["items"]
    assert json.loads(confirmed[-1]["raw_content"])["nodes"][0]["identity_status"] == "confirmed"
    old_version = confirmed[-1]["version_id"]
    with source.begin() as conn:
        conn.execute(text("UPDATE entity_dict SET is_deleted=true WHERE id=:id"), {"id": identity_id})
    assert publication.publish_once()["state"] == "published"
    second = history.list_changes("reviewed_graph", True)["items"]
    node = json.loads(second[-1]["raw_content"])["nodes"][0]
    assert second[-1]["version_id"] != old_version and node["identity_status"] == "unresolved"
    assert node["namespace"].endswith(".extracted_entity")
    assert confirmed[0]["raw_content"] == first[0]["raw_content"]


def test_low_disk_can_ack_known_history_commit_but_cannot_start_another_copy(
    databases, monkeypatch
):
    source, target = databases
    insert_document(source, snapshot("known-commit"))
    real = history.import_archive

    def committed_unknown(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("SYNTHETIC commit unknown")

    monkeypatch.setattr(history, "import_archive", committed_unknown)
    assert publication.publish_once()["state"] == "failed"
    monkeypatch.setattr(history, "import_archive", real)
    with source.begin() as conn:
        conn.execute(
            text("UPDATE publication_outbox SET next_attempt_at=clock_timestamp()")
        )
    insert_document(source, snapshot("do-not-copy"))
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_MIN_FREE_BYTES", str(2**62))
    assert (
        publication.publish_once()["state"] == "published"
    )  # Existing identical receipt only.
    assert publication.publish_once()["state"] == "resource_blocked"
    assert version_count(target) == 1
    pending = rows(
        source, "SELECT snapshot FROM publication_outbox WHERE state='pending'"
    )
    assert pending and pending[0]["snapshot"]["document"]["raw_content"]


def test_restart_cannot_relabel_frozen_real_and_demo_scope(databases, monkeypatch):
    source, target = databases
    insert_document(source, snapshot("scope-frozen"))
    row = publication._claim(source)
    publication._prepare(source, row)
    with source.begin() as conn:
        conn.execute(
            text(
                "UPDATE publication_outbox SET lease_until=clock_timestamp()-interval '1 second'"
            )
        )
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_IS_DEMO", "false")
    result = publication.publish_once()
    assert (
        result["state"] == "blocked"
        and result["error"]["code"] == "publication_demo_scope_changed"
    )
    assert version_count(target) == 0


@pytest.mark.parametrize("change", ["update", "delete"])
def test_backfill_waits_for_concurrent_candidate_then_never_republishes_old_cache(
    databases, change
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    source, _target = databases
    value = snapshot("SYNTHETIC-concurrent-stock")
    insert_document(source, value)
    with source.begin() as conn:
        conn.execute(text("DELETE FROM publication_outbox"))
    waiting = Event()

    def before_cursor(_conn, _cursor, statement, _params, _context, _many):
        if (
            "pg_advisory_xact_lock" in statement
            or "SELECT publication_record_document" in statement
        ):
            waiting.set()

    event.listen(source, "before_cursor_execute", before_cursor)
    pool = ThreadPoolExecutor(max_workers=1)
    with source.connect() as writer:
        transaction = writer.begin()
        try:
            if change == "update":
                newer = snapshot(status="TERMINATED")
                writer.execute(
                    text("UPDATE documents SET raw_content=:body WHERE uuid=:uuid"),
                    {"body": newer["raw_content"], "uuid": value["uuid"]},
                )
            else:
                writer.execute(
                    text("DELETE FROM documents WHERE uuid=:uuid"),
                    {"uuid": value["uuid"]},
                )
            future = pool.submit(publication.backfill_once, source, limit=1)
            assert waiting.wait(3), (
                "Backfill did not reach the old-row/new-candidate lock boundary"
            )
            transaction.commit()
            result = future.result(timeout=10)
        finally:
            if transaction.is_active:
                transaction.rollback()
            pool.shutdown(wait=True)
            event.remove(source, "before_cursor_execute", before_cursor)
    assert result["seeded"] == 0
    captured = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE kind='document' ORDER BY id",
    )
    assert len(captured) == 1
    if change == "update":
        assert (
            captured[0]["snapshot"]["document"]["raw_content"] == newer["raw_content"]
        )
    else:
        assert captured[0]["snapshot"]["document"]["is_deleted"] is True


def test_quarantined_source_and_its_waiting_successor_do_not_stop_authorized_stock(
    databases,
):
    source, target = databases
    stock = snapshot("SYNTHETIC-authorized-stock")
    insert_document(source, stock)
    with source.begin() as conn:
        conn.execute(text("DELETE FROM publication_outbox"))
    unknown = snapshot("SYNTHETIC-quarantined")
    unknown["source_site"] = "unknown"
    insert_document(source, unknown)
    assert publication.publish_once()["state"] == "blocked"
    with source.begin() as conn:
        conn.execute(
            text(
                "UPDATE documents SET raw_content=raw_content || ' later source revision' WHERE uuid=:uuid"
            ),
            {"uuid": unknown["uuid"]},
        )
    result = publication.backfill_once(limit=1)
    assert result["seeded"] == 1
    assert publication.publish_once()["state"] == "published"
    assert version_count(target) == 1
    waiting = rows(
        source,
        "SELECT state FROM publication_outbox WHERE source_doc_uuid='SYNTHETIC-quarantined' ORDER BY id",
    )
    assert [item["state"] for item in waiting] == ["blocked", "pending"]


def test_reprojection_same_original_makes_new_version_and_keeps_old_immutable(
    databases, monkeypatch
):
    from biz_rules.collected_document import collected_document
    from biz_rules.history_models import ArchiveImport, ArchiveRecord

    _source, target = databases
    value = snapshot("SYNTHETIC-reprojection")
    value["raw_content"] = (
        "---\ntitle: SYNTHETIC\nsource_site: clinicaltrials\ndoc_type: clinical_trial\nlanguage: en\n---\n\n"
        + value["raw_content"]
    )
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(history, "_now", lambda _conn: now)
    doc = collected_document(value, now, "insert")
    item = ArchiveRecord(
        source_record_id="legacy:clinicaltrials:" + value["uuid"],
        record_kind="trial",
        raw_content=doc.model_dump_json(),
    )
    request = ArchiveImport(
        source_id="legacy_documents",
        is_demo=True,
        scope_description="SYNTHETIC parser regression",
        declared_count=1,
        declared_complete=False,
        records=[item],
    )
    actual = history.project_record

    def old_projection(source_id, record):
        projected = actual(source_id, record)
        projected["parser_version"] = "datafabric.collected.document.projection.v0.1"
        projected["clean_text"] = value["raw_content"].split("## 原始数据")[0].rstrip()
        return projected

    with monkeypatch.context() as previous:
        previous.setattr(history, "project_record", old_projection)
        old_run = history.import_archive(request, "SYNTHETIC-original-v01")
    old = history.read_version(
        old_run.version_ids[0],
        "legacy_documents",
        item.source_record_id,
        True,
        "current",
        now,
    ).model_dump(mode="json")
    new_run = history.import_archive(request, "SYNTHETIC-reproject-v02")
    new = history.read_version(
        new_run.version_ids[0],
        "legacy_documents",
        item.source_record_id,
        True,
        "current",
        now,
    )
    assert new.version_id != old["version_id"]
    assert new.raw_content == old["raw_content"] and new.raw_sha256 == old["raw_sha256"]
    assert new.availability_proof.model_dump(mode="json") == old["availability_proof"]
    assert new.facts == old["facts"] and new.field_locators == old["field_locators"]
    assert new.parser_version.endswith("v0.2") and "source_site:" not in new.clean_text
    assert (
        history.read_version(
            old_run.version_ids[0],
            "legacy_documents",
            item.source_record_id,
            True,
            "current",
            now,
        ).model_dump(mode="json")
        == old
    )
    page = history.list_changes("legacy_documents", True)
    assert page["current_version_ids"][item.source_record_id] == new.version_id
    assert [row["version_id"] for row in page["items"]] == [
        old["version_id"],
        new.version_id,
    ]
    again = history.import_archive(request, "SYNTHETIC-reproject-v02")
    assert again.id == new_run.id and version_count(target) == 2


@pytest.mark.parametrize("unit", ["characters", "bytes"])
def test_oversized_input_is_blocked_for_one_hour_without_losing_fifo_or_other_documents(
    databases, unit
):
    from datetime import timedelta

    from biz_rules.history_models import MAX_RECORD_BYTES, sha256

    source, target = databases
    value = snapshot("SYNTHETIC-large-source")
    insert_document(source, value)
    prior = publication.publish_once()
    assert prior["state"] == "published"
    marker = "SYNTHETIC_VALUE_MUST_NOT_BE_RECORDED"
    oversized = marker + (
        "x" * MAX_RECORD_BYTES
        if unit == "characters"
        else "药" * (MAX_RECORD_BYTES // 3 + 1)
    )
    with source.begin() as conn:
        conn.execute(
            text("UPDATE documents SET raw_content=:body WHERE uuid=:uuid"),
            {"body": oversized, "uuid": value["uuid"]},
        )
    with source.begin() as conn:
        conn.execute(
            text("UPDATE documents SET raw_content=:body WHERE uuid=:uuid"),
            {
                "body": "SYNTHETIC later revision must wait for its predecessor.",
                "uuid": value["uuid"],
            },
        )
    insert_document(source, snapshot("SYNTHETIC-independent-normal"))
    result = publication.publish_once()
    assert result["state"] == "blocked"
    assert result["error"]["code"] == "publication_record_too_large"
    saved = rows(
        source,
        "SELECT * FROM publication_outbox WHERE state='blocked'",
    )[0]
    assert saved["error"] == result["error"]
    assert saved["error"]["field"] == "raw_content"
    assert (
        saved["error"]["limit"] == MAX_RECORD_BYTES and saved["error"]["unit"] == unit
    )
    assert marker not in json.dumps(saved["error"]) and "input_value" not in json.dumps(
        saved["error"]
    )
    assert saved["next_attempt_at"] - datetime.now(timezone.utc) > timedelta(minutes=59)
    assert saved["prepared"] is None and saved["receipt"] is None
    assert sha256(saved["snapshot"]["document"]["raw_content"]) == sha256(oversized)
    assert publication.publish_once()["state"] == "published"
    assert publication.publish_once()["state"] == "idle"
    states = rows(
        source,
        "SELECT state FROM publication_outbox WHERE source_doc_uuid='SYNTHETIC-large-source' ORDER BY id",
    )
    assert [item["state"] for item in states] == ["published", "blocked", "pending"]
    assert version_count(target) == 2
    old = history.read_version(
        prior["document_version_ids"][0],
        "legacy_documents",
        "legacy:clinicaltrials:" + value["uuid"],
        True,
        "current",
        datetime.now(timezone.utc),
    )
    assert json.loads(old.raw_content)["original_raw_sha256"] == sha256(
        value["raw_content"]
    )
    publication.publisher_heartbeat("running")
    assert publication.publication_status()["counts"]["blocked"] == 1

def test_secondary_source_evidence_reaches_a_real_published_graph(databases):
    from pathlib import Path
    from db.identity import record_evidence
    source, target = databases
    with source.begin() as conn:
        conn.exec_driver_sql((Path(__file__).resolve().parents[1] /
            "migration/versions/V023__identity_evidence.sql").read_text())
    values = [snapshot("evidence-first"), snapshot("evidence-second")]
    for value in values:
        value["raw_content"] = "SYNTHETIC company supplies this trial.\\n" + value["raw_content"]
        insert_document(source, value)
    adopted = {}
    for value in values:
        result = publication.publish_once()
        assert result["state"] == "published"
        record = history.read_version(result["document_version_ids"][0], "legacy_documents",
            "legacy:clinicaltrials:" + value["uuid"], True, "current", datetime.now(timezone.utc))
        adopted[value["uuid"]] = record
    with Session(source) as session, session.begin():
        target_dict = EntityDict(entity_type="Company", is_verified=True)
        session.add(target_dict); session.flush()
        entity = ExtractedEntity(source_doc_uuid=values[0]["uuid"], source_site="clinicaltrials",
            entity_type="Company", entity_name="SYNTHETIC company", source_text="SYNTHETIC company",
            review_status="approved", dict_id=target_dict.id)
        session.add(entity); session.flush()
        entity_id = entity.id
        for value in values:
            record = adopted[value["uuid"]]
            proof = {"version_id": record.version_id, "quote": "SYNTHETIC company",
                "char_start": record.clean_text.index("SYNTHETIC company"),
                "char_end": record.clean_text.index("SYNTHETIC company") + len("SYNTHETIC company")}
            record_evidence(session, "entity", entity, {"evidence": [proof]}, value["uuid"], "clinicaltrials")
    for _ in range(6):
        result = publication.publish_once()
        assert result["state"] in {"published", "idle"}
        if result["state"] == "idle":
            break
    graphs = history.list_changes("reviewed_graph", True)["items"]
    second = next(json.loads(item["raw_content"]) for item in graphs
        if json.loads(item["raw_content"])["selected_document_uuids"] == [values[1]["uuid"]])
    assert len(second["nodes"]) == 1
    assert second["nodes"][0]["identity_status"] == "unresolved"  # Old flags never become a fresh approval.
    assert second["nodes"][0]["evidence"][0]["version_id"] == adopted[values[1]["uuid"]].version_id
    with Session(source) as session, session.begin():
        record_evidence(session, "entity", session.get(ExtractedEntity, entity_id),
            {"evidence": [proof]}, values[1]["uuid"], "clinicaltrials")
    assert publication.publish_once()["state"] == "idle"
    assert len(history.list_changes("reviewed_graph", True)["items"]) == len(graphs)
