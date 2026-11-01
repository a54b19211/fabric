"""A0.7 —— 标识符通路修复测试。

覆盖三类缺陷及其修复：
  1. 落库容器错位：fetcher 把标识符写 structured_data，落库只读 extra，
     导致 pubmed 的 pmid / doi / pmcid 整批退化为 Pydantic 默认值。
     修复后 _merge_site_fields 按站点 Extra 模型的字段集补齐。
  2. 采集端脏值：NCBI 对 embargo 记录把元信息塞进 pmcid value
     （"PMCpmc-id: PMC13283083;embargo-date: ..."），旧实现整串照收。
  3. 对齐管线只覆盖药名：nct_id / pmid / doi 从未走本地格式校验，
     论文「标准标识符对齐」实际上只有一族在线通路。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_rules.extractors.pubmed.pubmed_fetcher import normalize_pmcid
from core_engine import storage_writer, transform_pipeline
from core_engine.authority_store import summarize
from core_engine.storage_writer import _merge_site_fields, write_to_database
from core_engine.transform_pipeline import apply_identifier_alignment


# ───────────────── 1. PMCID 归一 ─────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PMC13283083", "PMC13283083"),
        ("pmc13283083", "PMC13283083"),
        # 真实脏值：esummary 的 value 是「PMC 开头的键值串」，可能追加
        # embargo-date / manuscript-id 等附加键（存量 9 条有值记录里 7 条
        # 是不带附加键的纯键值串，2 条带附加键）
        ("PMCpmc-id: PMC13283083;embargo-date: 2027/07/01;", "PMC13283083"),
        ("pmc-id: PMC1234567;", "PMC1234567"),
        ("13283083", "PMC13283083"),
        ("PMC 13283083", "PMC13283083"),
        ("", ""),
        ("no-id-here", ""),
        (None, ""),
    ],
)
def test_normalize_pmcid(raw, expected):
    assert normalize_pmcid(raw) == expected


# ───────────────── 2. 落库容器合并 ─────────────────


def test_merge_fills_all_pubmed_identifiers():
    """pubmed 的核心标识符必须从 structured_data 补进 extra。"""
    structured = {
        "pmid": "42333167",
        "pmcid": "PMC13283083",
        "doi": "10.1007/s12288-025-02246-x",
        "journal_name": "Indian J Hematol",
        "authors": ["Garg P", "Gupta N"],
        "fulltext_status": "pmc_fulltext",
    }
    merged = _merge_site_fields({}, structured, "pubmed")

    assert merged["pmid"] == "42333167"
    assert merged["pmcid"] == "PMC13283083"
    assert merged["doi"] == "10.1007/s12288-025-02246-x"
    assert merged["journal_name"] == "Indian J Hematol"
    assert merged["authors"] == ["Garg P", "Gupta N"]
    assert merged["fulltext_status"] == "pmc_fulltext"


def test_merge_fills_clinicaltrials_structured_fields():
    merged = _merge_site_fields(
        {}, {"drug_name": "Docetaxel", "enrollment": 125}, "clinicaltrials"
    )
    assert merged["drug_name"] == "Docetaxel"
    assert merged["enrollment"] == 125


def test_merge_does_not_override_existing_extra():
    merged = _merge_site_fields(
        {"pmid": "999"}, {"pmid": "42333167"}, "pubmed"
    )
    assert merged["pmid"] == "999"


def test_merge_skips_blank_and_zero():
    merged = _merge_site_fields(
        {}, {"pmid": "  ", "enrollment": 0, "authors": []}, "pubmed"
    )
    assert "pmid" not in merged
    assert "authors" not in merged


def test_merge_rejects_type_mismatch():
    """drug_name 是 str 字段，列表值不得补入（避免下游类型错误）。"""
    merged = _merge_site_fields({}, {"drug_name": ["A", "B"]}, "clinicaltrials")
    assert "drug_name" not in merged


def test_merge_unknown_site_falls_back_to_whitelist():
    merged = _merge_site_fields({}, {"drug_name": "X", "pmid": "1"}, "no_such_site")
    assert merged["drug_name"] == "X"
    assert "pmid" not in merged


# ───────────────── 3. 标识符对齐（本地、零网络）─────────────────


def _parsed(extra=None, sd=None, site="clinicaltrials"):
    return {
        "uuid": f"u-{site}-a07",
        "source_site": site,
        "structured_data": sd if sd is not None else {},
        "extra": extra if extra is not None else {},
        "lineage": {},
    }


def test_identifier_alignment_hits_nct_locally(monkeypatch):
    """合法 NCT 号走本地校验即命中，不应触发任何联网解析。"""

    def _boom(*a, **k):  # pragma: no cover - 只在被误调用时触发
        raise AssertionError("标识符格式校验不应联网")

    monkeypatch.setattr(transform_pipeline, "_fetch_cids", _boom, raising=False)

    out = apply_identifier_alignment.fn(
        _parsed(extra={"nct_id": "NCT00488982", "drug_name": ""})
    )
    idres = out["identifier_resolutions"]

    assert idres["nct_id"]["found"] is True
    assert idres["nct_id"]["std_id"] == "NCT:00488982"
    assert idres["nct_id"]["source_id"] == "format"
    assert out["authority_resolutions"] == {}


def test_identifier_alignment_covers_pmid_and_doi():
    out = apply_identifier_alignment.fn(
        _parsed(
            extra={"pmid": "42333167", "doi": "10.1007/s12288-025-02246-x"},
            site="pubmed",
        )
    )
    idres = out["identifier_resolutions"]
    assert idres["pmid"]["std_id"] == "PMID:42333167"
    assert idres["doi"]["std_id"] == "DOI:10.1007/s12288-025-02246-x"


def test_identifier_alignment_records_invalid_format_as_miss():
    """中文批准文号不是 FDA 申请号格式，应如实记录未命中而非报错。"""
    out = apply_identifier_alignment.fn(
        _parsed(extra={"approval_number": "国药准字H20050021"}, site="nmpa")
    )
    res = out["identifier_resolutions"]["approval_number"]
    assert res["found"] is False
    assert res["raw_value"] == "国药准字H20050021"


def test_identifier_alignment_skips_empty_extra():
    out = apply_identifier_alignment.fn(_parsed(extra={}))
    assert out["identifier_resolutions"] == {}


def test_identifier_and_drug_name_stats_are_separate():
    """两族口径必须分开：格式校验命中不得计入药名命中率。"""
    drug = {"阿司匹林": {"found": True, "std_id": "PUBCHEM:2244"}}
    idents = {
        "nct_id": {"found": True, "std_id": "NCT:00488982"},
        "pmid": {"found": False, "std_id": ""},
    }
    assert summarize(drug)["match_rate"] == 1.0
    assert summarize(idents)["match_rate"] == 0.5
    # 混算会得到 2/3，掩盖药名 100% 的真实表现
    assert summarize({**drug, **idents})["match_rate"] != 1.0


# ───────────────── 4. 端到端：pubmed 落库 ─────────────────


def test_pubmed_envelope_carries_identifiers(monkeypatch):
    captured = {}

    def _fake_upsert(envelope):
        captured["env"] = envelope

    monkeypatch.setattr(storage_writer, "upsert_document", _fake_upsert)
    monkeypatch.setattr(storage_writer, "save_lineage", lambda pr: None)
    monkeypatch.setattr(storage_writer.APP, "ENABLE_LOCAL_STORAGE", False)

    parsed = {
        "uuid": "u-pubmed-a07",
        "source_site": "pubmed",
        "markdown": "正文 " * 40,
        "structured_data": {
            "title": "T",
            "pmid": "42333167",
            "doi": "10.1007/s12288-025-02246-x",
            "journal_name": "Indian J Hematol",
            "authors": ["Garg P"],
            "fulltext_status": "pmc_fulltext",
        },
        "sniffed_meta": {},
        "source_url": "https://pubmed.ncbi.nlm.nih.gov/42333167/",
        "attachment_links": [],
        "extra": {},
        "lineage": {},
        "identifier_resolutions": {
            "pmid": {"found": True, "std_id": "PMID:42333167", "skipped": False},
            "doi": {"found": True, "std_id": "DOI:10.1007/s12288-025-02246-x", "skipped": False},
        },
    }

    res = write_to_database.fn(parsed)
    extra = captured["env"].extra

    assert extra["pmid"] == "42333167"
    assert extra["doi"] == "10.1007/s12288-025-02246-x"
    assert extra["fulltext_status"] == "pmc_fulltext"
    assert res["identifier_total"] == 2
    assert res["identifier_matched"] == 2
    assert res["identifier_match_rate"] == 1.0
