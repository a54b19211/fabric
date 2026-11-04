"""T-28 ④ 整链：本地 PDF 上传 / PubMed PMC 全文 → history 版本 → 附件关系 → derived_text 解析。

真实 PostgreSQL 隔离 schema（沿用 integration_history 的 pg 夹具：127.0.0.1:18436/fabric_history，
每次建临时 schema 跑完删除）；MinerU 换成替身，对外 HTTP 用 MockTransport，不联网。
运行（一行）：DATAFABRIC_HISTORY_DATABASE_URL=postgresql+psycopg://<user>:<pw>@127.0.0.1:18436/fabric_history
      PYTHONPATH=/tmp/itest-deps:. <venv>/bin/python -m pytest -p no:cacheprovider tests/integration_local_files.py
"""

import hashlib
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from api.routers.history import router
from biz_rules import history_sources
from biz_rules.history_models import FetchRequest
from core_engine import scrapling_client as client
from db import history as store
from integration_history import pg, pg_engine  # noqa: F401  同一隔离 schema 夹具
from pdf_fixture import make_pdf
from test_pubmed_fulltext import CC_BY, Web, jats

ADMIN = "test-admin-only"


@pytest.fixture
def chain(pg, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["local_files","pubmed"]')
    monkeypatch.setattr(client.APP, "DATA_ROOT", tmp_path)
    monkeypatch.delenv("DATAFABRIC_UNPAYWALL_EMAIL", raising=False)
    monkeypatch.delenv("DATAFABRIC_PUBMED_FULLTEXT", raising=False)
    monkeypatch.delenv("DATAFABRIC_MINERU_CLOUD_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def upload(test_client, content, key, name="报告.pdf"):
    return test_client.post(
        "/api/history/local-files",
        headers={"Authorization": "Bearer " + ADMIN, "Idempotency-Key": key},
        files={"file": (name, content, "application/pdf")},
        data={"collection": "t28-evidence", "is_demo": "true"},
    )


def test_uploaded_pdf_is_a_fixed_version_and_is_parsed_with_page_locators(chain, pg, monkeypatch):  # noqa: F811
    from db import derived_text
    from processing import attachment_parse as ap

    pdf = make_pdf("Local import evidence")
    first = upload(chain, pdf, "local-1")
    assert first.status_code == 200, first.text
    run = first.json()
    assert run["kind"] == "archive" and run["state"] == "completed" and len(run["version_ids"]) == 1
    version = run["version_ids"][0]
    assert upload(chain, pdf, "local-1").json()["id"] == run["id"]  # 同幂等键：同一次导入
    assert upload(chain, pdf, "local-2").json()["version_ids"] == [version]  # 同字节：同一版本
    now = datetime.now(timezone.utc)
    record = store.read_version(version, "local_files", "local-file:t28-evidence/报告.pdf", True, "current", now)
    assert record.facts["sha256"] == hashlib.sha256(pdf).hexdigest()
    monkeypatch.setattr(ap, "_mineru", lambda p: {
        "success": True, "markdown": "Local import evidence",
        "content_list": [{"type": "text", "text": "Local import evidence", "page_idx": 0}]})
    assert derived_text.process(batch=10, sources={"local_files"}, retry_after_seconds=0) == {
        "parsed": 1, "failed": 0, "unsupported": 0}
    got = derived_text.for_versions("local_files", True, [version], "current", datetime.now(timezone.utc))
    entry = got["versions"][version]
    assert entry["status"] == "parsed" and entry["locator_kind"] == "pdf_page"
    assert entry["segments"][0]["page"] == 1 and "attachment_of" not in entry  # 本地原件没有父页面
    second = upload(chain, make_pdf("Second revision"), "local-3").json()["version_ids"][0]
    assert second != version  # 同名新字节：同一记录的新版本


def test_pubmed_pmc_fulltext_links_to_its_same_run_article_and_parses_as_jats(chain, pg, monkeypatch):  # noqa: F811
    from db import derived_text

    Web(monkeypatch, pmc=jats(CC_BY))  # 替身网络
    request = FetchRequest(source_id="pubmed", query="x", source_record_ids=["111", "222"])
    run, _ = store.create_fetch(request, "pubmed-fulltext-1")
    store.job_input(run.id, run.attempt)
    batch = history_sources.read_public(request)
    done = store.finish_fetch(run.id, run.attempt, batch)
    assert done.state == "completed" and len(done.version_ids) == 3
    assert {c["status"] for c in done.coverage["attachment_coverage"]} >= {"stored", "no_pmcid"}
    article, attachment = done.version_ids[0], done.version_ids[1]  # 全文紧跟它的文献
    with pg.begin() as conn:
        link = conn.execute(text(
            "SELECT parent_record_id,parent_version_id,link_basis FROM history_attachment_links "
            "WHERE attachment_version_id=:a"), {"a": attachment}).mappings().one()
    assert dict(link) == {"parent_record_id": "111", "parent_version_id": article,
                          "link_basis": "same_run_parent_version"}
    assert derived_text.process(batch=10, sources={"pubmed"}, retry_after_seconds=0) == {
        "parsed": 1, "failed": 0, "unsupported": 0}
    got = derived_text.for_versions("pubmed", False, [attachment], "current", datetime.now(timezone.utc))
    entry = got["versions"][attachment]
    assert entry["status"] == "parsed" and entry["locator_kind"] == "jats_section"
    assert entry["attachment_of"][0]["parent_version_id"] == article
