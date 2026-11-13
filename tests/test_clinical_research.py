"""Existing-record research access; synthetic data and no service connections."""

import hashlib
import json
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Query, Session

from api.main import app
from api.routers import research
from graph.extraction.clinical_research import MAX_DOCUMENT_BYTES, project_clinical_trial

DOCUMENT_ID = "00000000-0000-4000-8000-000000000001"


def protocol():
    return {
        "identificationModule": {"nctId": "NCT00000001", "briefTitle": "Synthetic trial"},
        "descriptionModule": {"briefSummary": "A synthetic summary, not medical evidence."},
        "conditionsModule": {"conditions": ["Synthetic condition"]},
        "armsInterventionsModule": {
            "interventions": [
                {"type": "DRUG", "name": "Synthetic BCMA-001", "description": "not adopted as a name"},
            ]
        },
        "designModule": {"phases": ["PHASE1"]},
        "statusModule": {
            "overallStatus": "TERMINATED",
            "whyStopped": "Synthetic administrative reason",
            "startDateStruct": {"date": "2019-04", "type": "ESTIMATED"},
            "lastUpdatePostDateStruct": {"date": "2026-09-01", "type": "ACTUAL"},
        },
    }


def body(value=None):
    value = protocol() if value is None else value
    # The old prose is deliberately wrong; facts must come from the appendix.
    return (
        "# 教学合成资料\n开始日期：2026-09-01\n研究涉及药物/干预：wrong disease\n"
        "## 研究摘要\nA short old summary.\n---\n## 原始数据\n"
        "**protocolSection**: " + json.dumps(value, ensure_ascii=False) + "\n**hasResults**: True\n"
    )


def test_projection_uses_exact_unicode_source_slice_and_original_field_values():
    raw = body()
    projection = project_clinical_trial(raw)
    assert projection.status == "available"
    assert raw[projection.locator.start : projection.locator.end] == projection.text
    assert projection.locator.offset_unit == "unicode_codepoint"
    assert json.loads(projection.text) == protocol()
    assert projection.facts.interventions[0].name == "Synthetic BCMA-001"
    assert projection.facts.brief_summary != "A short old summary."
    assert projection.facts.start_date.model_dump() == {"date": "2019-04", "type": "ESTIMATED"}
    assert projection.facts.last_update_post_date.date == "2026-09-01"
    assert projection.field_paths["interventions"] == "/protocolSection/armsInterventionsModule/interventions"
    assert projection.field_paths["why_stopped"] == "/protocolSection/statusModule/whyStopped"
    assert "hasResults" not in projection.facts.model_dump()


def test_missing_fields_remain_unknown_and_real_empty_arrays_are_retained():
    value = {"identificationModule": {"nctId": "NCT00000002"}, "designModule": {"phases": []}}
    facts = project_clinical_trial(body(value)).facts
    assert facts.conditions is None
    assert facts.interventions is None
    assert facts.phases == []
    assert facts.why_stopped is None
    assert facts.start_date is None


@pytest.mark.parametrize(
    "raw",
    [
        "No source appendix",
        body() + "\n## 原始数据\n",
        body() + "\n**protocolSection**: {}\n",
        body().replace('"NCT00000001"', '"not-a-registry-id"'),
        body().replace(
            '"overallStatus": "TERMINATED"', '"overallStatus": "TERMINATED", "overallStatus": "COMPLETED"'
        ),
        body().replace('"name": "Synthetic BCMA-001"', '"name": 12'),
        body({**protocol(), "armsInterventionsModule": {"interventions": {}}}),
        body().replace('"briefSummary": "A synthetic summary, not medical evidence."', '"briefSummary": NaN'),
        body().replace("**protocolSection**: {", "**protocolSection**: broken {"),
        "x" * (MAX_DOCUMENT_BYTES + 1),
    ],
)
def test_unusable_or_ambiguous_sources_never_become_empty_successful_facts(raw):
    projection = project_clinical_trial(raw)
    assert projection.status == "unavailable"
    assert projection.reason
    assert projection.text is None and projection.facts is None and projection.locator is None


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("research tests cannot connect to a service")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def database(monkeypatch):
    # Build the actual PostgreSQL queries, replacing only query execution.
    session = Session()
    state = SimpleNamespace(rows=[], row=None, queries=[], setup=[], failure=None)
    monkeypatch.setattr(session, "execute", lambda statement: state.setup.append(str(statement)))

    def execute_query(query, result):
        compiled = query.statement.compile(dialect=postgresql.dialect())
        state.queries.append((str(compiled), compiled.params))
        if state.failure is not None:
            raise state.failure
        return result

    monkeypatch.setattr(Query, "all", lambda query: execute_query(query, state.rows))
    monkeypatch.setattr(Query, "first", lambda query: execute_query(query, state.row))

    @contextmanager
    def get_session():
        yield session

    monkeypatch.setattr(research, "get_session", get_session)
    yield state
    session.close()


def row(raw=None):
    raw = body() if raw is None else raw
    return SimpleNamespace(
        uuid=DOCUMENT_ID,
        title="Synthetic trial",
        source_site="clinicaltrials",
        source_url="https://example.invalid/synthetic",
        publish_date="2026-09-01",
        crawl_timestamp=None,
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        raw_bytes=len(raw.encode()),
        raw_content=raw,
    )


def test_literal_title_and_search_has_a_bounded_stable_query_and_no_count(database):
    database.rows = [row(), row(), row()]
    with TestClient(app) as client:
        response = client.get(
            "/api/research/documents/search",
            params=[("terms", " BCMA%_\\ "), ("terms", "myeloma"), ("limit", "2")],
        )
    assert response.status_code == 200
    data = response.json()
    assert data["scope"] == "title_literal_and_current_db"
    assert data["terms"] == ["BCMA%_\\", "myeloma"]
    assert data["has_more"] and len(data["items"]) == 2
    assert "total" not in data and "raw_content" not in data["items"][0]
    sql, parameters = database.queries[0]
    assert "documents.source_site =" in sql and "clinicaltrials" in parameters.values()
    assert "is_deleted IS false" in sql and "ORDER BY documents.id ASC" in sql
    assert "COUNT(" not in sql.upper() and "OFFSET" not in sql.upper()
    assert "%BCMA\\%\\_\\\\%" in parameters.values()
    assert "%myeloma%" in parameters.values() and 3 in parameters.values()
    assert "documents.raw_content" not in sql
    assert database.setup == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '3000ms'"]


def test_no_matches_are_successful_only_after_a_completed_query(database):
    with TestClient(app) as client:
        response = client.get("/api/research/documents/search", params={"terms": "synthetic"})
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["has_more"] is False
    assert len(database.queries) == 1


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"terms": " "},
        {"terms": "x" * 129},
        {"terms": "x", "limit": "0"},
        {"terms": "x", "limit": "21"},
        [("terms", str(i)) for i in range(6)],
    ],
)
def test_invalid_search_stops_before_database_access(database, parameters):
    with TestClient(app) as client:
        assert client.get("/api/research/documents/search", params=parameters).status_code == 422
    assert database.queries == [] and database.setup == []


def test_read_document_returns_frozen_source_fields_not_wrong_legacy_prose(database):
    database.row = row()
    with TestClient(app) as client:
        response = client.get("/api/research/documents/" + DOCUMENT_ID)
    assert response.status_code == 200
    data = response.json()
    assert data["document_sha256"] == hashlib.sha256(body().encode()).hexdigest()
    assert data["publish_date_raw"] == "2026-09-01"
    assert data["projection"]["facts"]["start_date"]["date"] == "2019-04"
    assert data["crawl_timestamp"] is None
    assert "first_seen_at" not in data and "available_at" not in data
    sql, parameters = database.queries[0]
    assert "CASE WHEN" in sql and MAX_DOCUMENT_BYTES in parameters.values()
    assert "is_deleted IS false" in sql and "clinicaltrials" in parameters.values()
    assert str(UUID(DOCUMENT_ID)) in parameters.values()


@pytest.mark.parametrize("case,status", [("missing", 404), ("oversized", 413), ("unparseable", 200)])
def test_missing_large_and_unparseable_documents_remain_distinct(database, case, status):
    if case != "missing":
        database.row = row("No valid appendix")
        if case == "oversized":
            database.row.raw_bytes = MAX_DOCUMENT_BYTES + 1
            database.row.raw_content = None
    with TestClient(app) as client:
        response = client.get("/api/research/documents/" + DOCUMENT_ID)
    assert response.status_code == status
    if case == "unparseable":
        assert response.json()["projection"]["status"] == "unavailable"
        assert response.json()["projection"]["facts"] is None


@pytest.mark.parametrize("path", ["/search?terms=synthetic", "/" + DOCUMENT_ID])
def test_database_failure_is_not_empty_and_does_not_expose_exception_text(database, path):
    database.failure = OperationalError("private SQL", {}, RuntimeError("private password and source body"))
    with TestClient(app) as client:
        response = client.get("/api/research/documents" + path)
    assert response.status_code == 503
    assert "items" not in response.json()
    assert "private" not in response.text


def test_database_not_configured_is_not_a_successful_empty_query(monkeypatch):
    @contextmanager
    def get_session():
        yield None

    monkeypatch.setattr(research, "get_session", get_session)
    with TestClient(app) as client:
        assert client.get("/api/research/documents/search?terms=synthetic").status_code == 503


@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode"])
def test_statement_timeout_is_an_explicit_failure_for_both_postgres_drivers(database, attribute):
    original = RuntimeError("private database detail")
    setattr(original, attribute, "57014")
    database.failure = OperationalError("private SQL", {}, original)
    with TestClient(app) as client:
        response = client.get("/api/research/documents/search?terms=synthetic")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "research_query_timeout"
    assert "private" not in response.text


def test_four_terms_can_retain_a_seed_and_add_a_followup_constraint(database):
    terms = ["BCMA", "myeloma", "synthetic", "BCMA-001"]
    with TestClient(app) as client:
        response = client.get("/api/research/documents/search", params=[("terms", t) for t in terms])
    assert response.status_code == 200
    assert response.json()["terms"] == terms
    assert all("%" + term + "%" in database.queries[0][1].values() for term in terms)
