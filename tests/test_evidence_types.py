"""证据类型唯一注册表（contracts.evidence_types）与三处引用（来源目录、画像、业务验收）的一致性。

审阅 396/395：v3 1.4 的十二类是业务证据大类；registry_record、filing_fact 等是细类，各自映射到大类；
metadata/abstract/fulltext 是内容覆盖层级，不和证据类别混用；没有合适大类的细类保留并标 unmapped。
纯本地测试，不联网、不连库。
"""

import unicodedata
from pathlib import Path

import pytest

from collection.catalog import CATALOG, SPECS
from contracts import acceptance as acc
from contracts import evidence_types as et
from processing import profile as pf

ROOT = Path(__file__).resolve().parent.parent


def test_categories_are_the_twelve_of_v3_1_4_and_acceptance_uses_them():
    assert list(et.CATEGORIES) == [
        "registry", "regulatory_decision", "primary_research", "preprint", "conference", "company_disclosure",
        "financial", "policy", "official_notice", "media", "social", "patent",
    ]
    assert acc.EVIDENCE_TYPES is et.CATEGORIES


def test_every_fine_type_maps_to_a_category_or_says_unmapped():
    allowed = set(et.CATEGORIES) | {et.UNMAPPED, et.UNKNOWN}
    for slug, t in et.FINE_TYPES.items():
        assert t.category in allowed, slug
        if slug in et.CATEGORIES:  # 同名细类就是该大类本身
            assert t.category == slug, slug
    expected = {
        "registry_record": "registry", "regulatory_letter": "regulatory_decision",
        "regulatory_approval": "regulatory_decision", "regulatory_label": "regulatory_decision",
        "regulatory_notice": "regulatory_decision", "filing_fact": "financial",
        "corporate_filing": "company_disclosure", "industry_media": "media",
        "conference_abstract": "conference", "literature_unknown_type": "unknown",
    }
    assert {k: et.category_of(k) for k in expected} == expected


def test_unmapped_fine_types_are_kept_not_forced_into_primary_research():
    assert set(et.unmapped_fine_types()) == {
        "review", "literature_other", "guideline", "drug_database", "measurement", "internal_ledger",
        "funding",
    }
    for slug in et.unmapped_fine_types():
        assert et.category_of(slug) == "unmapped" != "primary_research"
    assert et.category_of("not_a_type") == "unknown"
    # 一组细类覆盖的大类：去重保序，unmapped 照样列出
    assert et.categories_of(["primary_research", "review", "preprint", "guideline"]) == [
        "primary_research", "unmapped", "preprint"]


def test_coverage_levels_are_separate_from_evidence():
    assert {"metadata", "abstract", "fulltext"} <= set(et.COVERAGE)
    assert not set(et.COVERAGE) & (set(et.FINE_TYPES) | set(et.CATEGORIES) | set(et.DOC_ROLES))
    assert not set(et.DOC_ROLES) & set(et.FINE_TYPES)


def test_registry_self_check_rejects_mixing(monkeypatch):
    monkeypatch.setitem(et.FINE_TYPES, "abstract", et.FineType("摘要", "primary_research", None))
    with pytest.raises(ValueError, match="覆盖层级"):
        et.check_registry()
    monkeypatch.delitem(et.FINE_TYPES, "abstract")
    monkeypatch.setitem(et.FINE_TYPES, "media", et.FineType("媒体", "social", None))
    with pytest.raises(ValueError, match="同名"):
        et.check_registry()


def test_catalog_uses_only_registered_fine_types_and_coverage_levels():
    for s in SPECS:
        assert s.evidence_types, s.source_id
        for t in s.evidence_types:
            assert t in et.FINE_TYPES, (s.source_id, t)
        assert set(s.coverage) <= set(et.COVERAGE), s.source_id
    # 覆盖层级不再写成证据类型
    levels = {"abstract", "literature_metadata", "metadata", "fulltext"}
    for s in SPECS:
        assert not levels & set(s.evidence_types), s.source_id
    assert CATALOG["europe_pmc"].coverage["abstract"]
    assert CATALOG["conference_abstracts"].evidence_types == ("conference_abstract",)
    # 一个来源可以覆盖多类
    epmc = et.categories_of(CATALOG["europe_pmc"].evidence_types)
    assert epmc == ["primary_research", "unmapped", "preprint"]


def run(source, **kw):
    return pf.profile(source_id=source, role=kw.get("role"), chars=kw.get("chars", 800), warnings=[],
                      identifiers=[], pub_types=kw.get("raw_types"))


def test_profile_credibility_and_doc_types_come_from_the_registry():
    assert pf.PROCESSOR_VERSION == "datafabric.profile.v3"
    assert pf.CREDIBILITY == {k: t.credibility for k, t in et.FINE_TYPES.items() if t.credibility}
    allowed = set(et.FINE_TYPES) | set(et.DOC_ROLES) | {et.UNKNOWN}
    raw_types = (None, ["review"], ["research-article"], ["preprint"], ["editorial"])
    for sid in CATALOG:
        for role in (None, "attachment"):
            for raw in raw_types:
                p = run(sid, role=role, raw_types=raw)
                assert p["doc_type"] in allowed, (sid, role, raw, p["doc_type"])
                assert p["doc_category"] in set(et.CATEGORIES) | {et.UNMAPPED, et.UNKNOWN}
                assert p["evidence_categories"] == et.categories_of(p["evidence_types"])
    assert run("legacy_documents")["doc_type"] == "stored_document"


@pytest.mark.parametrize("source,kw,doc_type,category", [
    ("clinicaltrials", {}, "registry_record", "registry"),
    ("europe_pmc", {"raw_types": ["research-article"]}, "primary_research", "primary_research"),
    ("europe_pmc", {"raw_types": ["review"]}, "review", "unmapped"),
    ("europe_pmc", {"raw_types": ["preprint"]}, "preprint", "preprint"),
    ("europe_pmc", {"raw_types": None}, "literature_unknown_type", "unknown"),
    ("sec_companyfacts", {}, "filing_fact", "financial"),
    ("cde_zdyz", {}, "guideline", "unmapped"),
    ("nmpa", {"role": "attachment"}, "attachment", "regulatory_decision"),  # 附件随所属来源
    ("europe_pmc", {"role": "attachment"}, "attachment", "unknown"),  # 来源覆盖多类时附件判不出
    ("legacy_documents", {}, "stored_document", "unknown"),
    ("local_files", {}, "uploaded_document", "unknown"),  # 上传原件类别未判定，不猜
    ("some_future_source", {}, "unknown", "unknown"),
])
def test_profile_gives_category_alongside_doc_type(source, kw, doc_type, category):
    p = run(source, **kw)
    assert (p["doc_type"], p["doc_category"]) == (doc_type, category)


def test_profile_lists_categories_of_the_source():
    assert run("europe_pmc")["evidence_categories"] == ["primary_research", "unmapped", "preprint"]
    assert run("drugbank")["evidence_categories"] == ["unmapped"]
    assert run("some_future_source")["evidence_categories"] == []


def _width(line):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line)


@pytest.mark.parametrize("path", [ROOT / "contracts" / "evidence_types.py", Path(__file__)])
def test_line_width_at_most_110(path):
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert _width(line) <= 110, (path.name, no, _width(line))
