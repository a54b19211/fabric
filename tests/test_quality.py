"""徐子雯的洗字段、编号对齐、五维打分（clean/fields.py、drug_names.py、align.py、quality.py）：不联网、不连库。
用例取自她 11-13 版单测里的真实写法；分数按她的公式手算。"""

from datetime import date

import pytest

from clean import align, drug_names, fields, quality, quality_stage
from store.history_models import make_uuid

TODAY = date(2026, 11, 14)


def test_drug_name_candidates_follow_her_rules():
    assert drug_names.candidates("Ifosfamide Injection 3g/60 mL, Single dose vial, Rx only, NDC 0069-4496-22")[0] == (
        "Ifosfamide", "cut_descriptor")
    assert ("Etanercept", "inner_alias") in drug_names.candidates("Enbrel (Etanercept)")
    assert [v for v, _ in drug_names.candidates("Nivolumab + Relatlimab")] == ["Nivolumab + Relatlimab", "Nivolumab"]
    assert drug_names.candidates("Placebo") == []
    assert drug_names.candidates("阿司匹林肠溶片") == [("阿司匹林肠溶片", "cjk_as_is")]
    assert drug_names.names_of({"structured_data": {"drug_name": "ASPIRIN; PLACEBO；Aspirin"}}) == ["ASPIRIN", "PLACEBO",
                                                                                                  "Aspirin"]


def test_format_checks_include_the_cas_check_digit():
    assert align.check_format("cas_number", "50-78-2")["std_id"] == "CAS:50-78-2"  # 阿司匹林
    assert not align.check_format("cas_number", "50-78-3")["found"]
    assert align.check_format("application_number", "NDA 021436")["std_id"] == "NDA:021436"
    assert not align.check_format("nct_id", "NCT1234567")["found"]


def test_the_four_strategies_choose_and_all_authorities_down_is_degraded():
    pubchem = align.result("aspirin", "pubchem", "PUBCHEM:2244", "2-acetyloxybenzoic acid", 0.6)
    local = align.result("aspirin", "local", "LOCAL:aspirin", "阿司匹林", 0.7)
    assert align.source_hierarchy([local, pubchem])["std_id"] == "PUBCHEM:2244"  # 层级先于置信度
    assert align.structural_criterion([local, pubchem])["std_id"] == "PUBCHEM:2244"
    assert align.majority_vote([local, pubchem, local])["std_id"] == "LOCAL:aspirin"
    assert align.evidence_weighted([local, pubchem])["std_id"] == "PUBCHEM:2244"  # 0.6×1.0 > 0.7×0.5
    down = align.source_hierarchy([align.result("x", "pubchem", degraded=True), align.result("x", "ols4", degraded=True)])
    assert down["degraded"] and not down["found"]


def test_alignment_stops_at_the_first_hit_and_when_every_authority_is_down():
    asked = []

    def etanercept_only(name):
        asked.append(name)
        return align.result(name, "chembl", "CHEMBL:CHEMBL1201572", "ETANERCEPT", 0.9) if name == "Etanercept" \
            else align.result(name, "chembl")

    names, _ = align.align({"extra": {"drug_name": "Enbrel (Etanercept); Placebo"}}, ask=[etanercept_only])
    hit = names["Enbrel (Etanercept)"]
    assert (hit["found"], hit["candidate"], hit["rule"], hit["candidates_tried"]) == (True, "Etanercept", "inner_alias", 2)
    assert names["Placebo"]["skipped"] and asked == ["Enbrel (Etanercept)", "Etanercept"]  # 占位名不查
    assert len(hit["sources"]) == 2  # 每个候选名各库的原始结果都留着，离线可换策略重算

    asked.clear()
    down = [lambda name: asked.append(name) or align.result(name, "pubchem", degraded=True), align.local]
    names, _ = align.align({"extra": {"drug_name": "Enbrel (Etanercept)"}}, ask=down)
    # 联网的库连不上（本地白名单照常答“没有”）：记降级，不再换名字；这样的版本这次不存
    assert names["Enbrel (Etanercept)"]["degraded"] and asked == ["Enbrel (Etanercept)"]
    assert quality_stage._lookup_failed(names)
    assert align.summarize(names) == {"total": 1, "eligible": 1, "skipped": 0, "matched": 0, "match_rate": 0.0}


def test_field_cleaning_keeps_chinese_punctuation_and_l3_converts_units():
    doc = fields.clean_fields({"title": "<b>公告</b>  一", "publish_date": "2026年11月3日", "source_site": "nmpa",
                               "structured_data": {"objective": "主要目的：ＡＢＣ１２３"}, "extra": {"x": ["  a  b "]},
                               "markdown": ""})
    assert doc["title"] == "公告 一" and doc["publish_date"] == doc["structured_data"]["publish_date"] == "2026-11-03"
    assert doc["structured_data"]["objective"] == "主要目的：ABC123" and doc["extra"]["x"] == ["a b"]
    assert (doc["doc_type"], doc["language"]) == ("regulatory_notice", "zh")
    out, changed = fields.normalize_l3({"structured_data": {"dosage": "500 mg", "drug_name": "Aspirin"}, "extra": {}})
    assert out["structured_data"] == {"dosage": "0.5 g", "drug_name": "阿司匹林"} and len(changed) == 2


def page_row(text, extra):
    url = "https://www.nmpa.gov.cn/xxgk/ggtg/a.html"
    return {"version_id": "a" * 64, "source_id": "nmpa", "is_demo": False, "source_record_id": url, "title": "关于XX的公告",
            "url": url, "facts": {"role": "web_capture", "parse_method": "css_extract", "extra": extra},
            "parser": "datafabric.site.web_capture.v1", "published": "2026-11-01", "updated": None, "summary": None,
            "raw": None, "text_pv": "datafabric.clean_text.v12", "text": text}


def test_a_web_page_is_scored_with_her_weights_and_its_clean_text_is_only_read():
    text = "# 关于XX的公告\n\n" + "国药准字H20234567 的注册证书予以注销。" * 6
    doc, got = quality_stage.score_one(page_row(text, {"drug_name": "", "approval_number": "国药准字H20234567",
                                                       "manufacturer": ""}), TODAY, ask=[])
    assert doc["markdown"] == text
    # 完整性 0.5 + 0.3×2/4 + 0.2；准确性 0.25×0.7 + 0.15×0.7 + 0.15 + 0.15×0.7 + 0.15×0.7（字段、数值、编号、单位都没有证据）；
    # 可追溯性 0.40 + 0.35×14/16 + 0.25×4/8（7 个追踪字段各有解析、清洗两步，extra.approval_number 只有对齐一步）
    assert got["dimensions"] == pytest.approx({"completeness": 0.85, "accuracy": 0.64, "timeliness": 1.0,
                                               "authority": 0.9, "traceability": 0.83125}, abs=1e-4)
    assert (got["score"], got["level"]) == (pytest.approx(0.8411, abs=1e-4), "高")
    assert got["violations"] == [] and got["confidence"]["reason"] == "来自已知官方站点"
    assert doc["uuid"] == make_uuid("https://www.nmpa.gov.cn/xxgk/ggtg/a.html")  # uuid5(网址)：和她旧库同一算法


def test_an_empty_old_page_without_address_is_only_given_the_level_quarantine():
    _, got = quality_stage.score_one({**page_row("", {}), "title": "", "url": "", "published": "1990"}, TODAY, ask=[])
    assert got["level"] == "隔离" and "综合质量分低于隔离阈值（只记等级，不拦截）" in got["issues"]
    assert {"field": "raw_content", "rule": "required", "message": "必填字段为空: raw_content"} in got["violations"]


def test_only_a_real_hit_counts_as_aligned():
    """她原版看 resolved_value：占位名、降级结果都填了原名，也算命中；这里要 found 且 std_id 非空。"""
    assert not quality.aligned({"authority_resolutions": {"Placebo": {"found": False, "std_id": None, "skipped": True,
                                                                      "resolved_value": "Placebo"}}})
    assert quality.aligned({"authority_resolutions": {"x": {"found": True, "std_id": "PUBCHEM:1"}}})
