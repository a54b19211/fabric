"""history-only 新材料进入唯一的抽取/待审链（T-28 ⑤，Codex 390/391）。真实两 schema PostgreSQL：
documents 库（抽取/审核主体）+ history 库。LLM 和消歧用替身，其余是生产代码。"""

from integration_history import _fetch_study
from integration_publication import databases  # noqa: F401  -- 两 schema 隔离库夹具
from sqlalchemy import text

from clean import clean_stage
from graph.db.models import ExtractedEntity

NCT = "NCT09997001"


def _setup(monkeypatch):
    import graph.api.documents as documents_api
    import graph.db.repository as repository
    import graph.db.session
    import graph.extraction.extraction_flow as flow
    import graph.extraction.task_queue as tq

    # 这些模块在导入时按名字绑定了 get_session：都指到本测试的隔离库会话
    for module in (flow, repository, documents_api):
        if hasattr(module, "get_session"):
            monkeypatch.setattr(module, "get_session", graph.db.session.get_session)

    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST",
                       '["clinicaltrials","legacy_documents","reviewed_graph","external_archive"]')
    monkeypatch.setattr(tq, "is_extraction_paused", lambda: False)
    monkeypatch.setattr(flow, "run_extraction_for_uuids", lambda uuids: {"submitted": len(uuids)})
    monkeypatch.setattr(flow, "_disambiguate", lambda entities, session=None, **_: entities)
    seen = []

    def llm(chunk, source_site):
        seen.append(chunk.text)
        return {"entities": [{"entity_type": "ClinicalTrial", "entity_name": NCT, "source_text": NCT,
                              "confidence_score": 0.9}], "relations": []}

    monkeypatch.setattr(flow, "_call_llm_for_chunk", llm)
    return seen


def test_a_history_only_version_reaches_pending_review_and_v1_stays_v1(databases, monkeypatch):
    from graph.db.extraction_subjects import enqueue_version, subject_uuid
    from graph.extraction.extraction_rq_job import extraction_job

    source, _target = databases
    seen = _setup(monkeypatch)
    v1 = _fetch_study(NCT, "subj-v1", status="RECRUITING").version_ids[0]
    clean_stage.process()
    queued = enqueue_version("clinicaltrials", NCT, v1)
    assert queued["created"] and queued["uuid"] == subject_uuid("clinicaltrials", NCT, v1)

    # v2 到来（状态改了）并清洗完，v1 的任务还没跑
    v2 = _fetch_study(NCT, "subj-v2", status="COMPLETED").version_ids[0]
    clean_stage.process()
    assert v2 != v1

    assert extraction_job(queued["uuid"]) is True
    assert seen and all("RECRUITING" in s and "COMPLETED" not in s for s in seen)  # 读的是 v1 的清洗正文

    with source.begin() as conn:
        doc = conn.execute(text("SELECT raw_content, history_version_id, history_text_sha256, extraction_status "
                                "FROM documents WHERE uuid=:u"), {"u": queued["uuid"]}).mappings().one()
        assert doc["raw_content"] == "" and doc["history_version_id"] == v1  # 正文不复制，只存引用
        assert doc["history_text_sha256"] == queued["text_sha256"]
        ents = conn.execute(text("SELECT entity_name, review_status FROM extracted_entities "
                                 "WHERE source_doc_uuid=:u"), {"u": queued["uuid"]}).mappings().all()
        assert [(e["entity_name"], e["review_status"]) for e in ents] == [(NCT, "pending")]  # 待审候选
        # 这类主体的原件本来就在 history：不会再被冻结成 legacy_documents 的新版本
        outbox = conn.execute(text("SELECT count(*) FROM publication_outbox WHERE kind='document' "
                                   "AND source_doc_uuid=:u"), {"u": queued["uuid"]}).scalar()
        assert outbox == 0

    # 详情页按引用读回同一份清洗正文
    import graph.api.documents as documents_api

    detail = documents_api.get_document(queued["uuid"])
    assert detail.history_ref["version_id"] == v1 and "RECRUITING" in detail.raw_content


def test_enqueue_is_idempotent_and_refuses_uncleaned_or_mismatched_versions(databases, monkeypatch):
    import pytest

    from graph.db.extraction_subjects import enqueue_version
    from store.history_models import HistoryError

    _setup(monkeypatch)
    v1 = _fetch_study("NCT09997002", "subj-idem").version_ids[0]
    with pytest.raises(HistoryError) as error:
        enqueue_version("clinicaltrials", "NCT09997002", v1)  # 还没清洗
    assert error.value.code == "subject_not_cleaned"
    clean_stage.process()
    first = enqueue_version("clinicaltrials", "NCT09997002", v1)
    again = enqueue_version("clinicaltrials", "NCT09997002", v1)
    assert first["created"] and not again["created"] and first["uuid"] == again["uuid"]
    with pytest.raises(HistoryError):
        enqueue_version("clinicaltrials", "NCT00000000", v1)


def test_reviewed_results_on_a_history_subject_publish_a_graph_pointing_at_that_version(databases, monkeypatch):
    import json

    from sqlalchemy.orm import Session

    from graph.db.extraction_subjects import enqueue_version
    from graph.db.models import EntityDict
    from graph.extraction.extraction_rq_job import extraction_job
    from graph.publish import publication
    from store import history

    source, _target = databases
    _setup(monkeypatch)
    v1 = _fetch_study(NCT, "subj-graph-v1", status="RECRUITING").version_ids[0]
    clean_stage.process()
    queued = enqueue_version("clinicaltrials", NCT, v1)
    assert extraction_job(queued["uuid"]) is True
    with Session(source) as session, session.begin():
        identity = EntityDict(entity_type="ClinicalTrial", is_verified=True)
        session.add(identity)
        session.flush()
        entity = session.query(ExtractedEntity).filter(ExtractedEntity.source_doc_uuid == queued["uuid"]).one()
        entity.review_status, entity.dict_id = "approved", identity.id  # 人工审核通过
    result = publication.publish_once()
    assert result["state"] == "published", result
    assert result["document_version_ids"] == [v1]
    graph = json.loads(history.list_changes("reviewed_graph", False)["items"][-1]["raw_content"])
    assert graph["dependency_source_ids"] == ["clinicaltrials"]
    [ref] = graph["document_refs"]
    assert (ref["source_id"], ref["version_id"], ref["origin_doc_uuid"]) == ("clinicaltrials", v1, queued["uuid"])
    [node] = graph["nodes"]
    [evidence] = node["evidence"]
    assert evidence["quote"] == NCT and evidence["version_id"] == v1
    assert evidence["locator"]["basis"] == "derived_clean_text"
    assert evidence["locator"]["text_sha256"] == queued["text_sha256"]
    # 没有冻结出 legacy_documents 版本
    assert not history.list_changes("legacy_documents", False)["items"]


def test_codex_402_revoked_source_is_not_served_listed_or_sent_to_the_extractor(databases, monkeypatch):
    """Codex 402 第 1 点：登记入队后来源撤权，任务、详情、列表都不能再读出或送出该正文。"""
    from datetime import datetime, timezone

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import graph.api.documents as documents_api
    from graph.db.extraction_subjects import enqueue_version
    from graph.extraction.extraction_rq_job import extraction_job
    from store import history
    from store.history_models import HistoryError

    source, target = databases
    seen = _setup(monkeypatch)
    version = _fetch_study(NCT, "codex-subject-revoke", status="RECRUITING").version_ids[0]
    clean_stage.process()
    queued = enqueue_version("clinicaltrials", NCT, version)
    with target.begin() as conn:
        conn.execute(text("UPDATE history_sources SET read_enabled=false WHERE source_id=:s"), {"s": "clinicaltrials"})
    try:
        history.read_version(version, "clinicaltrials", NCT, False, "current", datetime.now(timezone.utc))
    except HistoryError as exc:
        assert exc.code == "source_not_available"
    else:
        raise AssertionError("history 应拒绝撤权来源")
    assert extraction_job(queued["uuid"]) is False and not seen  # 没调用模型
    with source.begin() as conn:
        status, error = conn.execute(text("SELECT extraction_status, extraction_error FROM documents WHERE uuid=:u"),
                                     {"u": queued["uuid"]}).one()
    assert status == "failed" and "不可读取" in error
    app = FastAPI()
    app.include_router(documents_api.router)
    client = TestClient(app)
    detail = client.get("/api/documents/" + queued["uuid"])
    assert detail.status_code == 403 and "RECRUITING" not in detail.text
    listed = client.get("/api/documents", params={"page_size": 100}).json()
    assert queued["uuid"] not in {item["uuid"] for item in listed["items"]}


def test_codex_402_repeated_enqueue_reports_the_artifact_pinned_to_the_job(databases, monkeypatch):
    """Codex 402 第 4 点：清洗器升级后再次登记同一版本，回执给出任务实际绑定的产物（v6），新产物另行标出。"""
    from clean import clean_text
    from graph.db.extraction_subjects import enqueue_version

    source, _ = databases
    _setup(monkeypatch)
    version = _fetch_study(NCT, "codex-subject-processor", status="RECRUITING").version_ids[0]
    clean_stage.process()
    first = enqueue_version("clinicaltrials", NCT, version)
    real = clean_text.clean_registry

    def updated(*args, **kwargs):
        out = real(*args, **kwargs)
        out["text"] = out["text"].replace("RECRUITING", "recruiting")
        return out

    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "synthetic.clean_text.v7")
    monkeypatch.setattr(clean_text, "clean_registry", updated)
    clean_stage.process()
    again = enqueue_version("clinicaltrials", NCT, version)
    with source.begin() as conn:
        stored = conn.execute(text("SELECT history_text_version, history_text_sha256 FROM documents WHERE uuid=:u"),
                              {"u": again["uuid"]}).one()
    assert (again["text_processor_version"], again["text_sha256"]) == tuple(stored)
    assert again["text_processor_version"] == first["text_processor_version"] and again["created"] is False
    assert again["newer_text"]["text_processor_version"] == "synthetic.clean_text.v7"
