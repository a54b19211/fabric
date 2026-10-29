"""真实隔离PG组合回归；只模拟网页输入与质量前置步骤，不连接网页/模型。"""
import copy
import importlib
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic_settings import DotEnvSettingsSource


# 复用原有真实两schema夹具；导入配置时明确禁用.env读取。
_spec = importlib.util.spec_from_file_location(
    "_crawl_publication_fixture", Path(__file__).with_name("integration_publication.py")
)
_publication = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _publication
with patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}):
    _spec.loader.exec_module(_publication)
databases = _publication.databases


@pytest.mark.parametrize("crawl_mode", ["incremental", "full"])
def test_old_revision_new_record_unknown_ack_and_explicit_replay(
    databases, monkeypatch, tmp_path, crawl_mode
):
    assert os.environ.get("DB_HOST", "") == ""
    assert os.environ.get("PREFECT_SERVER_ALLOW_EPHEMERAL_MODE") == "false"
    source, target = databases
    monkeypatch.chdir(tmp_path)
    with patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}):
        main = importlib.import_module("main_etl")
    from flows import crawl_flow
    from core_engine.scrapling_client import make_uuid
    from db import history, publication

    assert main.APP.DB_HOST == ""
    monkeypatch.setattr(main.APP, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(main.APP, "ENABLE_LOCAL_STORAGE", True)

    # 本回归聚焦实际main消费/存储/发表链，清洗、归一、对齐与质量用固定结果。
    # process_and_store.fn、write_to_database.fn及其DB调用保持真实。
    for name in ("clean_content", "apply_l3_normalization", "apply_identifier_alignment"):
        monkeypatch.setattr(getattr(crawl_flow, name), "fn", lambda value: value)
    monkeypatch.setattr(crawl_flow.check_quality, "fn", lambda value: {**value, "quality_passed": True})

    old_url = "https://clinicaltrials.gov/study/NCT00000001"
    new_url = "https://clinicaltrials.gov/study/NCT00000002"
    old_uuid, new_uuid = make_uuid(old_url), make_uuid(new_url)

    def parsed(url, status="RECRUITING"):
        value = _publication.snapshot(make_uuid(url), status=status)
        raw = value["raw_content"]
        if url == new_url:
            raw = raw.replace("NCT00000001", "NCT00000002")
        return {
            "uuid": value["uuid"], "source_url": url, "source_site": "clinicaltrials",
            "title": value["title"], "markdown": raw, "publish_date": "2026-10",
            "timestamp": "2026-10-29T00:00:00Z", "parse_method": "synthetic-input",
            "doc_type": "clinical_trial", "language": "en", "attachment_links": [],
        }

    inputs = {old_url: parsed(old_url), new_url: parsed(new_url)}
    fetched = []

    def fetch(url, *_args, **_kwargs):
        fetched.append(url)
        return [copy.deepcopy(inputs[url])]

    monkeypatch.setattr(main.fetch_and_parse_url, "fn", fetch)
    monkeypatch.setattr(main, "make_driver", lambda *_args: SimpleNamespace(
        iter_detail_urls=lambda: iter([old_url, new_url])
    ))

    # 先由真实存储产生旧记录和本地断点标记，再固定已发表的旧版本。
    seed = main.crawl_detail_page.fn("clinicaltrials", {}, old_url, crawl_mode=crawl_mode)
    assert seed["stats"]["passed"] == 1
    assert (main.APP.JSON_DIR / "clinicaltrials" / f"{old_uuid}.json").is_file()
    assert publication.publish_once()["state"] == "published"
    old_ref = history.list_changes("legacy_documents", True)["items"][0]
    assert old_ref["facts"]["status"] == "RECRUITING"
    assert _publication.version_count(target) == 1

    inputs[old_url] = parsed(old_url, "TERMINATED")
    actual_writer = crawl_flow.write_to_database.fn
    ack_lost = False

    def save_then_lose_new_ack(value):
        nonlocal ack_lost
        result = actual_writer(value)
        if value["uuid"] == new_uuid and not ack_lost:
            assert result["success"] is True
            ack_lost = True
            raise RuntimeError("SYNTHETIC acknowledgement lost after durable save")
        return result

    monkeypatch.setattr(crawl_flow.write_to_database, "fn", save_then_lose_new_ack)
    fetched.clear()
    first = main.crawl_single_start_url.fn(
        "clinicaltrials", {"name": "SYNTHETIC fixed inputs", "max_pages": 1},
        "https://example.invalid/list", crawl_mode=crawl_mode,
    )
    assert fetched == [old_url, new_url]
    assert first["status"] == "partial"
    assert first["stats"] == {"total": 2, "passed": 1, "quarantined": 0, "failed": 1}
    assert {row["uuid"] for row in _publication.rows(source, "SELECT uuid FROM documents")} == {
        old_uuid, new_uuid
    }
    log = _publication.rows(source, "SELECT status,failed_count,error_message FROM crawl_log")[-1]
    assert log["status"] == "partial" and log["failed_count"] == 1
    assert "acknowledgement lost" in log["error_message"]

    for _ in range(2):
        assert publication.publish_once()["state"] == "published"
    assert _publication.version_count(target) == 3
    changes = history.list_changes("legacy_documents", True)["items"]
    old_changes = [item for item in changes if item["source_record_id"] == old_ref["source_record_id"]]
    assert [item["facts"]["status"] for item in old_changes] == ["RECRUITING", "TERMINATED"]
    assert len([item for item in changes if item["source_record_id"] != old_ref["source_record_id"]]) == 1

    # 显式重跑同批核对已提交的新记录，不把未知确认当作再次创建资料。
    replay = main.crawl_single_start_url.fn(
        "clinicaltrials", {"name": "SYNTHETIC replay", "max_pages": 1},
        "https://example.invalid/list", crawl_mode=crawl_mode,
    )
    assert replay["status"] == "success"
    assert replay["stats"] == {"total": 2, "passed": 2, "quarantined": 0, "failed": 0}
    assert publication.publish_once()["state"] == "idle"
    assert _publication.version_count(target) == 3
    fixed = history.read_version(
        old_ref["version_id"], "legacy_documents", old_ref["source_record_id"],
        True, "current", datetime.now(timezone.utc),
    )
    assert fixed.version_id == old_ref["version_id"]
    assert fixed.raw_sha256 == old_ref["raw_sha256"]
    assert fixed.facts["status"] == "RECRUITING"
