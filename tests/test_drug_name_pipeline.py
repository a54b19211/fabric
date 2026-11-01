"""A0：药名（drug_name）通路回归测试。

背景：药名在落库前一刻被丢掉，导致 T3 标识符对齐在存量数据上几乎无效。
本文件锁定四个环节的修复：

1. `SiteExtra` 基类统一声明 `drug_name`，`validate_extra()` 不再把它丢弃
2. `storage_writer._merge_site_fields()` 从 `structured_data` 补齐站点字段
   （CSS 抽取器写顶层 `extra`，API/fetcher 写 `structured_data`，两条路径并存）
3. frontmatter 生成同样认两个容器
4. 对齐结果区分「确认未命中 404」与「没查成（限流/网络）」，让命中率可测量
"""

import urllib.error

import pytest

from biz_rules.schemas import EXTRA_MODEL_MAP, SiteExtra, validate_extra
from core_engine import authority_client, content_cleaner as cc, storage_writer
from core_engine import transform_pipeline as tp
from core_engine.storage_writer import _merge_site_fields, write_to_database


# ── 1. extra 模型层：drug_name 是跨站点通用字段 ─────────────────────────────


def test_site_extra_base_declares_drug_name():
    assert "drug_name" in SiteExtra.model_fields


@pytest.mark.parametrize("site", sorted(EXTRA_MODEL_MAP))
def test_every_site_extra_keeps_drug_name(site):
    """任何站点模型经 validate_extra 后都必须保留 drug_name。"""
    out = validate_extra(site, {"drug_name": "阿司匹林"})
    assert out.get("drug_name") == "阿司匹林"


def test_validate_extra_defaults_missing_drug_name_to_empty():
    out = validate_extra("pubmed", {"pmid": "12345678"})
    assert out["drug_name"] == ""
    assert out["pmid"] == "12345678"


# ── 2. 落库层：两条产出路径合并 ────────────────────────────────────────────


def test_merge_fills_from_structured():
    merged = _merge_site_fields({}, {"drug_name": "Semaglutide"})
    assert merged["drug_name"] == "Semaglutide"


def test_merge_does_not_override_existing():
    merged = _merge_site_fields({"drug_name": "阿司匹林"}, {"drug_name": "aspirin"})
    assert merged["drug_name"] == "阿司匹林"


def test_merge_ignores_blank_structured_value():
    merged = _merge_site_fields({}, {"drug_name": "   "})
    assert "drug_name" not in merged


def test_merge_skips_non_string_value():
    merged = _merge_site_fields({}, {"drug_name": ["A", "B"]})
    assert "drug_name" not in merged


def _parsed(site="clinicaltrials", structured=None, extra=None):
    return {
        "uuid": f"u-{site}-a0",
        "source_site": site,
        "markdown": "正文内容 " * 40,
        "structured_data": structured or {},
        "sniffed_meta": {},
        "source_url": "https://example.com/a",
        "attachment_links": [],
        "extra": extra if extra is not None else {},
        "lineage": {},
    }


def _run_write(monkeypatch, parsed):
    captured = {}
    monkeypatch.setattr(storage_writer, "upsert_document",
                        lambda e: captured.setdefault("env", e))
    monkeypatch.setattr(storage_writer, "save_lineage", lambda pr: None)
    monkeypatch.setattr(storage_writer, "save_authority_resolutions", lambda pr: None)
    monkeypatch.setattr(storage_writer.APP, "ENABLE_LOCAL_STORAGE", False)
    res = write_to_database.fn(parsed)
    return res, captured["env"]


def test_api_path_drug_name_reaches_envelope(monkeypatch):
    """clinicaltrials：药名只在 structured_data，落库必须补进 extra。"""
    parsed = _parsed(
        site="clinicaltrials",
        structured={"title": "T", "drug_name": "Docetaxel; Melphalan"},
    )
    _res, env = _run_write(monkeypatch, parsed)
    assert env.extra["drug_name"] == "Docetaxel; Melphalan"
    assert env.model_dump()["extra"]["drug_name"] == "Docetaxel; Melphalan"


def test_css_path_drug_name_reaches_envelope(monkeypatch):
    """cde：药名在顶层 extra，不能被 structured_data 的空值覆盖。"""
    parsed = _parsed(
        site="cde",
        structured={"title": "T", "drug_name": ""},
        extra={"drug_name": "阿司匹林", "approval_number": "国药准字H12345678"},
    )
    _res, env = _run_write(monkeypatch, parsed)
    assert env.extra["drug_name"] == "阿司匹林"
    assert env.extra["approval_number"] == "国药准字H12345678"


def test_envelope_without_drug_name_stays_empty(monkeypatch):
    parsed = _parsed(site="pubmed", structured={"title": "T"})
    _res, env = _run_write(monkeypatch, parsed)
    assert not str(env.extra.get("drug_name") or "").strip()


# ── 3. frontmatter：两个容器都认 ────────────────────────────────────────────


def _clean(parsed):
    return cc.clean_content.fn(parsed)


def _frontmatter(markdown: str) -> str:
    if not markdown.startswith("---"):
        return ""
    idx = markdown.find("\n---", 3)
    return markdown[:idx] if idx > 0 else ""


def _base(**over):
    p = {
        "title": "标题",
        "markdown": "正文",
        "source_site": "adhoc_a0",
        "source_url": "https://example.com/a",
        "publish_date": "2026-01-01",
        "structured_data": {},
        "extra": {},
    }
    p.update(over)
    return p


def test_frontmatter_reads_structured_data():
    out = _clean(_base(structured_data={"drug_name": "OXCARBAZEPINE"}))
    assert 'drug_name: "OXCARBAZEPINE"' in _frontmatter(out["markdown"])


def test_frontmatter_reads_extra():
    out = _clean(_base(extra={"drug_name": "阿司匹林"}))
    assert 'drug_name: "阿司匹林"' in _frontmatter(out["markdown"])


def test_frontmatter_omits_line_when_no_drug_name():
    out = _clean(_base())
    assert "drug_name" not in _frontmatter(out["markdown"])


# ── 4. 对齐层：有药名才触发、多药拆分、绝不回退标题 ────────────────────────


def _fake_resolver(calls):
    def _fn(raw, timeout=5.0, id_type="drug_name"):
        calls.append(raw)
        return {"found": True, "std_id": f"PUBCHEM:{len(calls)}", "source_id": "pubchem",
                "raw_value": raw, "resolved_value": raw, "confidence": 0.9,
                "candidates": [], "degraded": False}
    return _fn


def test_alignment_triggers_on_structured_drug_name(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "resolve_identifier", _fake_resolver(calls))
    parsed = _parsed(structured={"drug_name": "Docetaxel; Melphalan"})
    out = tp.apply_identifier_alignment.fn(parsed)
    assert calls == ["Docetaxel", "Melphalan"]
    assert set(out["authority_resolutions"]) == {"Docetaxel", "Melphalan"}
    assert all(r["found"] for r in out["authority_resolutions"].values())


def test_alignment_triggers_on_top_level_extra(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "resolve_identifier", _fake_resolver(calls))
    parsed = _parsed(site="cde", extra={"drug_name": "阿司匹林"})
    out = tp.apply_identifier_alignment.fn(parsed)
    assert calls == ["阿司匹林"]
    assert "阿司匹林" in out["authority_resolutions"]


def test_alignment_never_falls_back_to_title(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "resolve_identifier", _fake_resolver(calls))
    parsed = _parsed()
    parsed["title"] = "关于某药品的公告"
    out = tp.apply_identifier_alignment.fn(parsed)
    assert calls == []
    assert out["authority_resolutions"] == {}


def test_alignment_caps_name_count(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "resolve_identifier", _fake_resolver(calls))
    parsed = _parsed(structured={"drug_name": "; ".join(f"Drug{i}" for i in range(12))})
    tp.apply_identifier_alignment.fn(parsed)
    assert len(calls) == tp._MAX_ALIGN_NAMES


# ── 5. 命中率可测量：区分 404 与「没查成」 ─────────────────────────────────


def _http_error(code):
    return urllib.error.HTTPError("https://pubchem", code, "err", {}, None)


def test_404_is_definitive_miss(monkeypatch):
    def _raise(_name, _timeout):
        raise _http_error(404)
    monkeypatch.setattr(authority_client, "_fetch_cids", _raise)
    res = authority_client.resolve_identifier("NotADrugXYZ")
    assert res["found"] is False
    assert res["degraded"] is False  # 权威库确认无此名，是有效结论


def test_network_failure_is_degraded(monkeypatch):
    def _raise(_name, _timeout):
        raise urllib.error.URLError("connection reset")
    monkeypatch.setattr(authority_client, "_fetch_cids", _raise)
    res = authority_client.resolve_identifier("Docetaxel")
    assert res["found"] is False
    assert res["degraded"] is True  # 没查成，不得计入命中率分母


def test_rate_limit_retries_once_then_succeeds(monkeypatch):
    seen = {"n": 0}

    def _fake_get(_url, _timeout):
        seen["n"] += 1
        if seen["n"] == 1:
            raise _http_error(429)
        return {"IdentifierList": {"CID": [148124]}}

    monkeypatch.setattr(authority_client, "_http_get_json", _fake_get)
    monkeypatch.setattr(authority_client.time, "sleep", lambda _s: None)
    cids = authority_client._fetch_cids("Docetaxel", 5.0)
    assert cids == [148124]
    assert seen["n"] == 2


def test_rate_limit_gives_up_after_one_retry(monkeypatch):
    def _always_429(_url, _timeout):
        raise _http_error(429)
    monkeypatch.setattr(authority_client, "_http_get_json", _always_429)
    monkeypatch.setattr(authority_client.time, "sleep", lambda _s: None)
    with pytest.raises(urllib.error.HTTPError):
        authority_client._fetch_cids("Docetaxel", 5.0)
