"""同URL附件更新的隔离PG回归；HTTP/MinerU替身，不访问网页或模型。"""

import importlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic_settings import DotEnvSettingsSource


# 复用现有真实双schema夹具，导入配置时禁止读取.env。
_spec = importlib.util.spec_from_file_location(
    "_attachment_publication_fixture",
    Path(__file__).with_name("integration_publication.py"),
)
_publication = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _publication
with patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}):
    _spec.loader.exec_module(_publication)
databases = _publication.databases


def test_same_url_attachment_refresh_and_failures_keep_original_and_history(
    databases, monkeypatch, tmp_path
):
    assert os.environ.get("DB_HOST", "") == ""
    assert os.environ.get("PREFECT_SERVER_ALLOW_EPHEMERAL_MODE") == "false"
    source, target = databases
    monkeypatch.chdir(tmp_path)
    with patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}):
        main = importlib.import_module("main_etl")
    from core_engine import router, scrapling_client
    from db import history, publication
    from flows import crawl_flow

    # route_pdf内部的规则任务也走真实.fn，避免启动/连接Prefect运行时。
    monkeypatch.setattr(router, "sniff_pdf_metadata", router.sniff_pdf_metadata.fn)
    assert main.APP.DB_HOST == ""
    monkeypatch.setattr(main.APP, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(main.APP, "ENABLE_LOCAL_STORAGE", False)
    monkeypatch.setattr(main.APP, "TASK_RETRIES", 0)
    site = "europe_pmc"  # 复用现有catalog；全部输入仍为SYNTHETIC、is_demo=True。
    permitted = json.loads(os.environ["DATAFABRIC_HISTORY_SOURCE_ALLOWLIST"])
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", json.dumps(permitted + [site]))
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_SOURCES", json.dumps([site]))
    monkeypatch.setitem(crawl_flow.SITE_PROFILES, site, {"merge_attachments": True})

    # 仅固定发现到的页面/附件、模型前置步骤；解析、存储和发布仍用生产方法。
    detail_url = "https://example.invalid/notice"
    pdf_url = "https://example.invalid/notice.pdf"
    doc_uuid = scrapling_client.make_uuid(detail_url)
    monkeypatch.setattr(main, "make_driver", lambda *_args: SimpleNamespace(
        iter_detail_urls=lambda: iter([detail_url])
    ))
    monkeypatch.setattr(crawl_flow.fetch_page, "fn", lambda **_kwargs: {"cookies": []})
    monkeypatch.setattr(crawl_flow.route_webpage, "fn", lambda *_args, **_kwargs: {
        "uuid": doc_uuid, "source_url": detail_url, "source_site": site,
        "title": "SYNTHETIC notice", "markdown": "", "structured_data": {},
        "attachment_links": [{"url": pdf_url, "name": "notice.pdf"}],
    })
    for name in ("clean_content", "apply_l3_normalization", "apply_identifier_alignment"):
        monkeypatch.setattr(getattr(crawl_flow, name), "fn", lambda value: value)
    monkeypatch.setattr(crawl_flow.check_quality, "fn", lambda value: {
        **value, "quality_passed": True,
    })

    originals = {
        marker: f"%PDF-1.4\nSYNTHETIC evidence {marker}\n%%EOF\n".encode()
        for marker in ("A", "B", "C")
    }
    state = {"body": originals["A"], "failure": None}
    requests, parsed_files = [], []
    real_client = httpx.Client

    def http_response(request):
        assert str(request.url) == pdf_url  # 任何意外HTTP调用都不得访问网络。
        requests.append(request)
        if state["failure"] == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC timeout", request=request)
        return httpx.Response(
            500 if state["failure"] == "http500" else 200,
            content=state["body"], headers={"content-type": "application/pdf"},
        )

    transport = httpx.MockTransport(http_response)
    monkeypatch.setattr(scrapling_client.httpx, "Client", lambda **kwargs:
                        real_client(transport=transport, **kwargs))

    def mineru_file(pdf_path):
        path = Path(pdf_path)
        content = path.read_bytes()
        parsed_files.append((path, content))
        if state["failure"] == "mineru":
            return {"success": False, "error": "SYNTHETIC MinerU rejected new body"}
        marker = next(key for key, value in originals.items() if content == value)
        return {"success": True,
                "markdown": f"# SYNTHETIC attachment\n\nSYNTHETIC evidence {marker}.\n"}

    monkeypatch.setattr(router.mineru_pdf_to_markdown, "fn", mineru_file)

    def crawl():
        return main.crawl_single_start_url.fn(
            site, {"name": "SYNTHETIC attachment refresh", "max_pages": 1},
            "https://example.invalid/list", crawl_mode="incremental",
        )

    def original_text(record):
        return json.loads(record["raw_content"])["original_raw_content"]

    def publish():
        result = publication.publish_once()
        assert result["state"] == "published", result

    first = crawl()
    assert first["status"] == "success" and first["stats"]["passed"] == 1
    publish()
    old = history.list_changes("legacy_documents", True)["items"][0]
    a_path = parsed_files[0][0]
    assert a_path.read_bytes() == originals["A"]
    assert "SYNTHETIC evidence A." in original_text(old)

    # 已有A缓存不能遮蔽读取失败；解析失败的新C也不得发表。
    for failure in ("timeout", "http500", "mineru"):
        state.update(failure=failure, body=originals["C"])
        parsed_before, requests_before = len(parsed_files), len(requests)
        result = crawl()
        assert result["status"] == "failed", failure
        assert result["stats"] == {"total": 1, "passed": 0, "quarantined": 0, "failed": 1}
        assert len(requests) == requests_before + 1
        assert len(parsed_files) == parsed_before + (failure == "mineru")
        log = _publication.rows(source, "SELECT status,error_message FROM crawl_log ORDER BY id DESC LIMIT 1")[0]
        assert log["status"] == "failed" and log["error_message"]
        assert publication.publish_once()["state"] == "idle"
        assert _publication.version_count(target) == 1
        head = history.list_records(
            "legacy_documents", True, "current", datetime.now(timezone.utc)
        ).items
        assert len(head) == 1 and head[0].version_id == old["version_id"]
        assert a_path.read_bytes() == originals["A"]

    state.update(body=originals["B"], failure=None)
    second = crawl()
    assert second["status"] == "success" and second["stats"]["passed"] == 1
    publish()
    b_path = parsed_files[-1][0]
    assert b_path != a_path and b_path.read_bytes() == originals["B"]
    replay = crawl()
    assert replay["status"] == "success" and replay["stats"]["passed"] == 1
    assert publication.publish_once()["state"] == "idle"
    assert parsed_files[-2:] == [(b_path, originals["B"]), (b_path, originals["B"])]
    assert len(requests) == 6 and _publication.version_count(target) == 2
    changes = history.list_changes("legacy_documents", True)
    assert len(changes["items"]) == 2
    current = history.list_records(
        "legacy_documents", True, "current", datetime.now(timezone.utc)
    ).items
    assert len(current) == 1 and current[0].version_id == changes["items"][1]["version_id"]
    assert "SYNTHETIC evidence B." in current[0].clean_text

    fixed = history.read_version(
        old["version_id"], "legacy_documents", old["source_record_id"],
        True, "current", datetime.now(timezone.utc),
    )
    assert fixed.version_id == old["version_id"] and fixed.raw_sha256 == old["raw_sha256"]
    assert "SYNTHETIC evidence A." in json.loads(fixed.raw_content)["original_raw_content"]
    assert a_path.read_bytes() == originals["A"] and b_path.read_bytes() == originals["B"]
    assert history.list_changes("legacy_documents", True)["current_version_ids"] == changes["current_version_ids"]
