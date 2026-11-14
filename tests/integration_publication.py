"""真实两schema PostgreSQL：实际原库写入触发待办再自动发表，不用SQLite或HTTP假写库。"""

import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from test_publication import snapshot

from graph.db.models import Base, Document
from graph.db.schemas import ConfidenceInfo, MetadataEnvelope, ProvenanceInfo
from graph.publish import publication
from store import records
from store.migrations import runner


@pytest.fixture
def databases(monkeypatch):
    url = os.environ.get("DATAFABRIC_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL required")
    parsed = make_url(url)
    assert (
        parsed.host == "127.0.0.1"
        and (parsed.port, parsed.database) in {(18436, "zk_fabric_test"), (18636, "zk_fabric_identity_test")}
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
    monkeypatch.setattr(records, "fabric_engine", lambda: target)
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_SOURCES", '["clinicaltrials"]')
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_IS_DEMO", "true")
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_NAMESPACE", "synthetic.publication")
    runner.apply("fabric", records.fabric_engine())
    assert runner.main(["--db", "publication", "--apply"]) == 0
    import graph.db.session

    @contextmanager
    def sessions():
        with Session(source) as session, session.begin():
            yield session

    monkeypatch.setattr(graph.db.session, "get_session", sessions)
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
    return rows(target, "SELECT count(*) n FROM fabric_versions")[0]["n"]


def test_source_transaction_rollback_and_a_committed_document_blocked_by_the_retired_bridge(databases):
    """Only a committed source write leaves an event. Publishing a document would write a new legacy_documents
    version: the bridge is retired, so the event is blocked with its reason (410) and kept; nothing is written."""
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
    assert (result["state"], result["error"]["code"]) == ("blocked", "legacy_retired")
    assert rows(source, "SELECT state FROM publication_outbox")[0]["state"] == "blocked"
    assert version_count(target) == 0 and publication.publish_once()["state"] == "idle"


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
    assert publication.publish_once()["state"] == "resource_blocked"
    publication.publisher_heartbeat("resource_blocked")
    status = publication.publication_status("legacy_documents")
    assert status["publisher_state"] == "resource_blocked" and status["last_heartbeat"]
    assert status["counts"]["blocked"] == 1
    assert version_count(target) == 0


def test_status_heartbeat_is_not_fabricated_and_revocation_is_not_idle(databases):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routers.fabric import router
    from graph.api.fabric_graph import router as fabric_graph_router

    source, target = databases
    app = FastAPI()
    app.include_router(router)
    app.include_router(fabric_graph_router)
    from unittest.mock import patch

    # Endpoint uses the same actual source/target engines, not a fake successful read.
    with (
        patch.dict(
            os.environ, {"DATAFABRIC_READ_TOKEN": "SYNTHETIC-read-only"}
        ),
        TestClient(app) as client,
    ):
        response = client.get(
            "/api/fabric/publication/status",
            params={"source_id": "legacy_documents"},
            headers={"Authorization": "Bearer SYNTHETIC-read-only"},
        )
        assert (
            response.status_code == 200
            and response.json()["publisher_state"] == "not_started"
        )
        assert response.json()["retired"] is True  # an audit reading of the old bridge, not a source to use
        no_default = client.get("/api/fabric/publication/status",
                                headers={"Authorization": "Bearer SYNTHETIC-read-only"})
        assert no_default.status_code == 422  # the source is chosen, never defaulted to legacy_documents
        publication.publisher_heartbeat("idle")
        result = client.get(
            "/api/fabric/publication/status",
            params={"source_id": "legacy_documents"},
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
        assert publication.publication_status("legacy_documents")["publisher_state"] == "stale"
        with target.begin() as conn:
            conn.execute(
                text(
                    "UPDATE fabric_sources SET read_enabled=false WHERE source_id='legacy_documents'"
                )
            )
        denied = client.get(
            "/api/fabric/publication/status",
            params={"source_id": "legacy_documents"},
            headers={"Authorization": "Bearer SYNTHETIC-read-only"},
        )
        assert denied.status_code == 403
        assert "counts" not in denied.json()


def test_extra_write_failure_rolls_back_document_and_capture_in_same_source_transaction(
    databases, monkeypatch
):
    from graph.db import repository

    source, target = databases
    value = snapshot("extra-rollback")

    def broken(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC extra insert failed after document flush")

    monkeypatch.setattr(repository.documents, "_upsert_doc_extra_in_session", broken)
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

