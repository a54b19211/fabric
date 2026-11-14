"""徐子雯的洗字段、编号对齐、五维打分（clean/fields.py、drug_names.py、align.py、quality.py）：不联网、不连库。
用例取自她 11-13 版单测里的真实写法；分数按她的公式手算。"""

from datetime import date

import pytest

from clean import align, drug_names, fields, quality, quality_stage
from ops import align_offline
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


def test_approval_numbers_are_checked_by_the_format_of_their_source():
    nmpa = align.check_identifiers({"source_site": "nmpa", "extra": {"approval_number": "国药准字H20234567"}})
    assert nmpa["approval_number"]["std_id"] == "NMPA:国药准字H20234567"  # 她原版按 NDA/ANDA/BLA 校验，必判不合格
    fda = align.check_identifiers({"source_site": "fda_api_enforcement", "extra": {"approval_number": "ANDA207088"}})
    assert fda["approval_number"]["std_id"] == "ANDA:207088"
    crl = align.check_identifiers({"source_site": "fda_crl", "extra": {"application_number": "NDA 214787; BLA761234"}})
    assert {k: v["std_id"] for k, v in crl.items()} == {"application_number": "NDA:214787",
                                                         "application_number#2": "BLA:761234"}


def test_one_authority_that_did_not_answer_makes_a_miss_degraded():
    """她原版 PubChem 故障就记降级、不进分母；别的库答“没有”也不能当确认没有。只是部分库没查成不算断网。"""
    def pubchem_down(name):
        return align.result(name, "pubchem", degraded=True)

    def chembl_none(name):
        return align.result(name, "chembl")

    names, _ = align.align({"extra": {"drug_name": "Ibuprofen; Nivolumab"}}, ask=[pubchem_down, chembl_none, align.local])
    assert names["Ibuprofen"]["degraded"] and names["Ibuprofen"]["authority"] == "pubchem"
    assert not align.all_down(names["Ibuprofen"]) and names["Nivolumab"]["candidates_tried"] == 1  # 第二个药名照样问
    assert quality_stage._lookup_failed(names)
    offline, _ = align.align({"extra": {"drug_name": "Ibuprofen"}}, ask=[align.local])  # 不出网的实例
    assert offline["Ibuprofen"]["degraded"] and align.all_down(offline["Ibuprofen"])


def test_an_authority_answering_an_odd_shape_counts_as_not_answered(monkeypatch):
    import httpx

    from fetch import rate, transport

    monkeypatch.setattr(rate, "wait", lambda group, interval: None)
    for body in (b"null", b"[]", b'"x"', b'{"IdentifierList": []}', b'{"molecules": [null]}', b'{"response": []}'):
        monkeypatch.setattr(transport, "get", lambda url, params=None, body=body, **k: httpx.Response(200, content=body))
        for lookup in (align.pubchem, align.chembl, align.ols4, align.uniprot):
            out = lookup("x")
            assert not out["found"], (body, lookup.__name__)


def test_the_four_strategies_choose_and_all_authorities_down_is_degraded():
    pubchem = align.result("aspirin", "pubchem", "PUBCHEM:2244", "2-acetyloxybenzoic acid", 0.6)
    local = align.result("aspirin", "local", "LOCAL:aspirin", "阿司匹林", 0.7)
    assert align.source_hierarchy([local, pubchem])["std_id"] == "PUBCHEM:2244"  # 层级先于置信度
    assert align_offline.structural_criterion([local, pubchem])["std_id"] == "PUBCHEM:2244"
    assert align_offline.majority_vote([local, pubchem, local])["std_id"] == "LOCAL:aspirin"
    assert align_offline.evidence_weighted([local, pubchem])["std_id"] == "PUBCHEM:2244"  # 0.6×1.0 > 0.7×0.5
    down = align.source_hierarchy([align.result("x", "pubchem", degraded=True), align.result("x", "ols4", degraded=True)])
    assert down["degraded"] and not down["found"]


def test_alignment_stops_at_the_first_hit_and_when_every_authority_is_down():
    asked = []

    def etanercept_only(name):
        asked.append(name)
        return align.result(name, "chembl", "CHEMBL:CHEMBL1201572", "ETANERCEPT", 0.9) if name == "Etanercept"             else align.result(name, "chembl")

    names, _ = align.align({"extra": {"drug_name": "Enbrel (Etanercept); Placebo"}}, ask=[etanercept_only])
    hit = names["Enbrel (Etanercept)"]
    assert (hit["found"], hit["candidate"], hit["rule"], hit["candidates_tried"]) == (True, "Etanercept", "inner_alias", 2)
    assert names["Placebo"]["skipped"] and asked == ["Enbrel (Etanercept)", "Etanercept"]  # 占位名不查
    assert len(hit["sources"]) == 2  # 每个候选名各库的原始结果都留着，离线可换策略重算
    align.align({"extra": {"drug_name": "Enbrel (Etanercept)"}}, ask=[etanercept_only])
    assert len(asked) == 2  # 同一个库、同一个候选名只问一次

    def pubchem_hit(name):
        return align.result(name, "pubchem", "PUBCHEM:5743", name, 0.9)

    names, _ = align.align({"extra": {"drug_name": "Dexamethasone"}}, ask=[pubchem_hit, etanercept_only])
    assert names["Dexamethasone"]["std_id"] == "PUBCHEM:5743" and len(asked) == 2  # PubChem 命中就不问后面的库

    asked.clear()
    down = [lambda name: asked.append(name) or align.result(name, "pubchem", degraded=True), align.local]
    names, _ = align.align({"extra": {"drug_name": "Enbrel (Etanercept); Nivolumab"}}, ask=down)
    # 联网的库连不上（本地白名单照常答“没有”）：记降级，不再换名字，第二个药名也不再问；这样的版本这次不存
    assert names["Enbrel (Etanercept)"]["degraded"] and names["Nivolumab"]["degraded"]
    assert asked == ["Enbrel (Etanercept)"] and quality_stage._lookup_failed(names)
    assert align_offline.summarize(names) == {"total": 2, "eligible": 2, "skipped": 0, "matched": 0, "match_rate": 0.0}


def test_field_cleaning_keeps_chinese_punctuation_and_l3_converts_units():
    doc = fields.clean_fields({"title": "<b>公告</b>  一", "publish_date": "2026年11月3日", "source_site": "nmpa",
                               "structured_data": {"objective": "主要目的：ＡＢＣ１２３"}, "extra": {"x": ["  a  b "]},
                               "markdown": ""})
    assert doc["title"] == "公告 一" and doc["publish_date"] == doc["structured_data"]["publish_date"] == "2026-11-03"
    assert doc["structured_data"]["objective"] == "主要目的：ABC123" and doc["extra"]["x"] == ["a b"]
    assert (doc["doc_type"], doc["language"]) == ("regulatory_notice", "zh")
    who = fields.clean_fields({"source_site": "who_don", "markdown": ""})
    assert (who["doc_type"], who["language"]) == ("regulatory_notice", "en")
    assert fields.clean_fields({"source_site": "local_files", "markdown": ""})["doc_type"] == ""  # 上传件不判类型
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


PUBMED_XML = ("<PubmedArticle><MedlineCitation><PMID>123</PMID><Article><Journal><Title>Nature medicine</Title></Journal>"
              "<ArticleTitle>T</ArticleTitle><AuthorList><Author><LastName>Smith</LastName><Initials>J</Initials></Author>"
              "<Author><CollectiveName>X Group</CollectiveName></Author></AuthorList></Article></MedlineCitation>"
              "</PubmedArticle>")


def test_a_pubmed_record_has_her_fifteen_keys_and_its_fulltext_status_from_its_attachments():
    row = {**page_row("T\n\nA", {}), "source_id": "pubmed", "source_record_id": "123", "raw": PUBMED_XML,
           "facts": {"pmid": "123", "doi": "10.1000/x", "pmcid": "PMC9", "abstract": "A",
                     "publication_types": ["Journal Article"]},
           "fulltext": [{"from": "pmc_efetch", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC9/"}]}
    doc = quality_stage.document(row)
    assert len(doc["structured_data"]) == 15 and doc["structured_data"]["fulltext_status"] == "pmc_fulltext"
    assert doc["extra"]["authors"] == ["Smith J", "X Group"] and doc["extra"]["journal_name"] == "Nature medicine"
    pdf = quality_stage.document({**row, "fulltext": [{"from": "unpaywall", "url": "https://x.org/a.pdf"}]})
    assert (pdf["extra"]["fulltext_status"], pdf["extra"]["oa_pdf_url"]) == ("unpaywall_pdf", "https://x.org/a.pdf")
    assert quality_stage.document({**row, "fulltext": None})["extra"]["fulltext_status"] == "abstract_only"


def test_odd_shapes_in_a_projection_are_read_as_empty_and_an_attachment_falls_back_to_its_file_name():
    for facts in (None, {"role": "web_capture", "extra": ["a"]}, {"role": "site_record", "structured": "ab"}):
        quality_stage.score_one({**page_row("x", {}), "facts": facts}, TODAY, ask=[])
    trial = {**page_row("x", {}), "source_id": "clinicaltrials", "source_record_id": "NCT01234567",
             "facts": {"interventions": [{"type": "DRUG", "name": 5}], "lead_sponsor": "X", "phase": "PHASE1"}}
    doc = quality_stage.document(trial)
    assert (doc["extra"]["phase"], doc["extra"]["sponsor"], doc["structured_data"]["drug_name"]) == (["PHASE1"], "", "5")
    attachment = quality_stage.document({**page_row("没有标题的正文", {}), "title": "附件1.pdf", "facts": {"role": "attachment"}})
    assert attachment["title"] == attachment["structured_data"]["title"] == "附件1.pdf"


def test_only_a_real_hit_counts_as_aligned():
    """她原版看 resolved_value：占位名、降级结果都填了原名，也算命中；这里要 found 且 std_id 非空。"""
    assert not quality.aligned({"authority_resolutions": {"Placebo": {"found": False, "std_id": None, "skipped": True,
                                                                      "resolved_value": "Placebo"}}})
    assert quality.aligned({"authority_resolutions": {"x": {"found": True, "std_id": "PUBCHEM:1"}}})
