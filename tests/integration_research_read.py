"""Explicit real PostgreSQL checks in a disposable schema; no upstream DB access."""
import json
import os
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from api.routers import research
from db.models import Document
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


@pytest.fixture
def database(monkeypatch):
    url = os.environ.get("DATAFABRIC_RESEARCH_TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.fail("A dedicated PostgreSQL URL is required; this check cannot be skipped")
    schema = "fabric_research_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
    Document.__table__.create(engine)

    @contextmanager
    def get_session():
        with Session(engine) as session, session.begin():
            yield session

    monkeypatch.setattr(research, "get_session", get_session)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def record(identifier, title, **extra):
    protocol = {"identificationModule": {"nctId": "NCT00000001"},
                "statusModule": {"overallStatus": "TERMINATED", "whyStopped": "Synthetic reason"}}
    raw = "测试前缀\n## 原始数据\n**protocolSection**: " + json.dumps(protocol) + "\n"
    return Document(uuid=identifier, title=title, source_site=extra.pop("source_site", "clinicaltrials"),
                    raw_content=extra.pop("raw_content", raw), **extra)


def test_real_query_scope_order_literal_search_and_source_slice(database):
    identifiers = [str(uuid4()) for _ in range(4)]
    with Session(database) as session, session.begin():
        session.add_all([
            record(identifiers[0], "Synthetic BCMA myeloma 100% evidence"),
            record(identifiers[1], "Synthetic BCMA myeloma_ evidence"),
            record(identifiers[2], "Synthetic BCMA myeloma excluded", source_site="other"),
            record(identifiers[3], "Synthetic BCMA myeloma deleted", is_deleted=True),
        ])
    first = research.search_documents(terms=["BCMA", "myeloma"], limit=1)
    assert first.has_more is True and first.items[0].uuid == identifiers[0]
    assert len(research.search_documents(terms=["BCMA", "myeloma"], limit=6).items) == 2
    assert [x.uuid for x in research.search_documents(terms=["100%"], limit=6).items] == identifiers[:1]
    assert [x.uuid for x in research.search_documents(terms=["myeloma_"], limit=6).items] == identifiers[1:2]
    assert not research.search_documents(terms=["not found"], limit=6).items
    detail = research.read_document(UUID(identifiers[0]))
    assert detail.projection.facts.overall_status == "TERMINATED"
    with Session(database) as session:
        raw = session.query(Document.raw_content).filter(Document.uuid == identifiers[0]).scalar()
    locator = detail.projection.locator
    assert raw[locator.start:locator.end] == detail.projection.text
    for identifier in identifiers[2:]:
        with pytest.raises(HTTPException) as error:
            research.read_document(UUID(identifier))
        assert error.value.status_code == 404


def test_real_oversized_body_and_bad_projection_are_not_empty_success(database):
    large, invalid = str(uuid4()), str(uuid4())
    with Session(database) as session, session.begin():
        session.add_all([
            record(large, "Synthetic huge", raw_content="x" * (2 * 1024 * 1024 + 1)),
            record(invalid, "Synthetic invalid", raw_content="no protocol"),
        ])
    with pytest.raises(HTTPException) as error:
        research.read_document(UUID(large))
    assert error.value.status_code == 413
    assert research.read_document(UUID(invalid)).projection.status == "unavailable"
    with Session(database) as session:
        research._configure_read(session)
        assert session.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        with pytest.raises(Exception) as failure:
            session.execute(Document.__table__.insert().values(uuid=str(uuid4())))
        assert getattr(failure.value.orig, "pgcode", None) == "25006"
        session.rollback()
