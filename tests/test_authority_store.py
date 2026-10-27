"""
T3 标准标识符对齐结果旁路存档测试（B3 过渡方案）。

背景：对齐结果的正式落库形态是 doc_authority_resolution 表（方案 2），
但该迁移需与服务器上未同步的 V017–V019 合并后才能执行，故当前先把结果写入
data/authority/<site>/<uuid>.json，并让信封与落库返回值携带统计。

本文件不连接数据库、不写真实 data 目录（用 tmp_path 重定向）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from biz_rules.schemas import MetadataEnvelope
from core_engine import authority_store, storage_writer
from core_engine.authority_store import (
    load_authority_resolutions,
    save_authority_resolutions,
    summarize,
)


def _res(found=True, std_id="PUBCHEM:2244", degraded=False, skipped=False):
    res = {
        "found": found,
        "std_id": std_id,
        "source_id": "2244",
        "raw_value": "aspirin",
        "resolved_value": "acetylsalicylic acid" if found else "aspirin",
        "confidence": 0.9 if found else 0.0,
        "candidates": [],
        "degraded": degraded,
    }
    if skipped:
        res["skipped"] = True
    return res


def _parsed(uuid="u-auth-001", site="pubmed", resolutions=None):
    return {
        "uuid": uuid,
        "source_site": site,
        "markdown": "正文内容 " * 50,
        "structured_data": {"title": "T", "publish_date": "", "category": "", "summary": ""},
        "sniffed_meta": {},
        "source_url": "https://pubmed.ncbi.nlm.nih.gov/1/",
        "attachment_links": [],
        "extra": {},
        "lineage": {},
        "authority_resolutions": resolutions if resolutions is not None else {"aspirin": _res()},
    }


# ────────────────────────── summarize ──────────────────────────
def test_summarize_empty():
    empty = {"total": 0, "eligible": 0, "skipped": 0, "matched": 0, "match_rate": 0.0}
    assert summarize({}) == empty
    assert summarize(None) == empty


def test_summarize_excludes_placeholder_from_denominator():
    """占位名（安慰剂）跳过查询，不能算作「未命中」稀释命中率。

    2026-10-27 变更：新增 eligible/skipped 两键。安慰剂本来就不该送权威库，
    若计入分母会系统性低估对齐能力（真实数据里 Placebo 占 3/28）。
    """
    res = {
        "aspirin": _res(found=True, std_id="PUBCHEM:2244"),
        "placebo": _res(found=False, std_id="", skipped=True),
    }
    stat = summarize(res)
    assert stat["total"] == 2
    assert stat["skipped"] == 1
    assert stat["eligible"] == 1
    assert stat["matched"] == 1
    assert stat["match_rate"] == 1.0


def test_summarize_counts_only_found_with_std_id():
    """found 为真但 std_id 为空、或未命中，都不算匹配。"""
    res = {
        "a": _res(found=True, std_id="PUBCHEM:2244"),
        "b": _res(found=False, std_id=""),
        "c": _res(found=True, std_id=""),          # 命中但无标识符 → 不计
        "d": _res(found=False, std_id="", degraded=True),
    }
    stat = summarize(res)
    assert stat["total"] == 4
    assert stat["matched"] == 1
    assert stat["match_rate"] == 0.25


# ────────────────────────── 存档读写 ──────────────────────────
def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(authority_store, "authority_dir", lambda: tmp_path)

    path = save_authority_resolutions(_parsed())
    assert path and Path(path).exists()

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["uuid"] == "u-auth-001"
    assert payload["source_site"] == "pubmed"
    assert payload["summary"] == {
        "total": 1, "eligible": 1, "skipped": 0,
        "matched": 1, "match_rate": 1.0,
    }
    assert payload["resolutions"]["aspirin"]["std_id"] == "PUBCHEM:2244"

    back = load_authority_resolutions("u-auth-001", "pubmed")
    assert back == payload


def test_save_skips_when_no_resolution(tmp_path, monkeypatch):
    """没有药名时不产生空文件，避免污染全库对齐率统计。"""
    monkeypatch.setattr(authority_store, "authority_dir", lambda: tmp_path)

    assert save_authority_resolutions(_parsed(resolutions={})) == ""
    assert list(tmp_path.rglob("*.json")) == []


def test_save_skips_without_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(authority_store, "authority_dir", lambda: tmp_path)
    parsed = _parsed(uuid="")

    assert save_authority_resolutions(parsed) == ""
    assert list(tmp_path.rglob("*.json")) == []


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(authority_store, "authority_dir", lambda: tmp_path)
    assert load_authority_resolutions("not-exist", "pubmed") is None


# ────────────────────────── 信封字段 ──────────────────────────
def test_envelope_carries_authority_resolutions():
    """信封新增字段默认空 dict，可携带对齐结果序列化。"""
    base = dict(uuid="u-env-001", raw_content="x" * 200)
    empty = MetadataEnvelope(**base)
    assert empty.authority_resolutions == {}

    env = MetadataEnvelope(**base, authority_resolutions={"aspirin": _res()})
    dumped = json.loads(env.model_dump_json())
    assert dumped["authority_resolutions"]["aspirin"]["std_id"] == "PUBCHEM:2244"


def test_upsert_does_not_read_authority_field(monkeypatch):
    """upsert_document 按字段白名单写入，信封多出该字段不影响落库。"""
    captured = {}
    monkeypatch.setattr(storage_writer, "upsert_document", lambda env: captured.setdefault("env", env))
    monkeypatch.setattr(storage_writer, "save_lineage", lambda pr: None)
    monkeypatch.setattr(storage_writer, "save_authority_resolutions", lambda pr: "")
    storage_writer.APP.ENABLE_LOCAL_STORAGE = False

    res = storage_writer.write_to_database.fn(_parsed())

    assert res["success"] is True
    # 信封带上了对齐结果
    assert captured["env"].authority_resolutions["aspirin"]["found"] is True
    # 落库返回值同时给出对齐统计，供上层汇总全库对齐率
    assert res["authority_total"] == 1
    assert res["authority_matched"] == 1
    assert res["authority_match_rate"] == 1.0


def test_write_reports_zero_when_no_resolution(monkeypatch):
    monkeypatch.setattr(storage_writer, "upsert_document", lambda env: None)
    monkeypatch.setattr(storage_writer, "save_lineage", lambda pr: None)
    monkeypatch.setattr(storage_writer, "save_authority_resolutions", lambda pr: "")
    storage_writer.APP.ENABLE_LOCAL_STORAGE = False

    res = storage_writer.write_to_database.fn(_parsed(resolutions={}))
    assert (res["authority_total"], res["authority_matched"], res["authority_match_rate"]) == (0, 0, 0.0)
