"""
药名归一测试（T3 前置步骤，2026-10-27）。

背景：A0 打通药名通路后，18 条真实信封实测文档级命中只有 4/18（22.2%）。
失败原因不是取不到药名，而是「药名不是药名」——
    fda_api_enforcement 的 drug_name 实际是 product_description（整段产品描述）
    clinicaltrials 是「试验代号 + 剂量」和「A + B」组合
    品牌名把通用名放在括号里
本文件的用例全部取自真实信封原文，防止规则被「想象的数据」带偏。

全文件不联网：只测候选名生成，不调用 resolve_identifier。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_rules.drug_name_normalizer import (  # noqa: E402
    MAX_CANDIDATES_PER_NAME,
    generate_candidates,
    is_placeholder,
    pick_primary,
)


def _values(raw: str) -> list:
    return [c.value for c in generate_candidates(raw)]


def _rules(raw: str) -> dict:
    return {c.value: c.rule for c in generate_candidates(raw)}


# ══════════════════════ 一、真实数据的四类形态 ══════════════════════
def test_product_description_is_cut_at_dosage_form():
    """fda_api_enforcement 真实原文：整段产品描述，应在剂型处截断。"""
    raw = ("Ifosfamide Injection 3g/60 mL, Single dose vial, Rx only, Sterile, "
           "For Intravenous Use, Refrigerate at 2-8 degrees celcius, "
           "Distributed by Pfizer Labs, Division of Pfizer Inc., New York, NY 10017, "
           "NDC 0069-4496-22")
    assert _values(raw)[0] == "Ifosfamide"
    assert _rules(raw)["Ifosfamide"] == "cut_descriptor"


def test_leading_dose_is_removed_including_ratio_unit():
    """剂量出现在句首时要整块吃掉（含 /mL 分母），否则会残留 'mL Fentanyl'。"""
    raw = ("2 mcg/mL Fentanyl Citrate and 0.16% Bupivacaine HCl (Preservative Free) "
           "in 0.9% Sodium Chloride, 100 mL in 150 mL Baxter Intravia Bag, "
           "Service Code 2K8102, Rx Only, Compounded by PharMEDium Services, LLC")
    values = _values(raw)
    assert values[0] == "Fentanyl Citrate"
    assert all(not v.startswith("mL") for v in values)


def test_brand_name_generic_in_parentheses_is_used_as_alias():
    """品牌名把通用名放括号里：Enbrel (Etanercept) → Etanercept。"""
    values = _values("Enbrel (Etanercept)")
    assert values[0] == "Enbrel (Etanercept)"     # 原值仍先尝试，保持可比基线
    assert "Etanercept" in values
    assert _rules("Enbrel (Etanercept)")["Etanercept"] == "inner_alias"


def test_trademark_marker_parenthetical_is_not_an_alias():
    """'(R)' 只是注册商标标记，不能当别名；真正的通用名在第二组括号里。"""
    raw = ("VIRACEPT (R) (nelfinavir mesylate) Tablets, 625 mg, 120-count bottle, "
           "Rx only, Manufactured by: Agouron Pharmaceuticals, Inc.")
    values = _values(raw)
    assert "R" not in values
    assert "nelfinavir mesylate" in values
    assert "VIRACEPT" in values


def test_combo_drug_yields_first_component():
    """'A + B' 组合：整体一定查不到，拆出首个成分才可能命中。"""
    values = _values("Nivolumab + Relatlimab")
    assert values == ["Nivolumab + Relatlimab", "Nivolumab"]
    assert _rules("Nivolumab + Relatlimab")["Nivolumab"] == "combo_split"
    assert "Docetaxel" in _values("Docetaxel and GM-CSF")


def test_trial_code_keeps_code_and_drops_dose():
    """试验代号 + 剂量：代号本身是有效名，剂量是杂质。"""
    assert _values("JNJ-26866138 0.7 mg/m2") == ["JNJ-26866138"]


def test_comma_segment_picks_real_substance():
    """包装名在前、真药名在后：'No Drip Nasal Spray, Oxymetazoline HCl 0.05% ...'。"""
    raw = ("No Drip Nasal Spray, Oxymetazoline HCl 0.05% Nasal Decongestant, "
           "1 FL Oz (30 mL) per bottle, Distributed by SUPERVALU INC.")
    assert "Oxymetazoline HCl" in _values(raw)


# ══════════════════════ 二、不该动的情况 ══════════════════════
def test_clean_name_produces_single_candidate():
    """本来就是规范名时不做任何展开，避免无谓请求。"""
    assert _values("OXCARBAZEPINE") == ["OXCARBAZEPINE"]
    assert _rules("OXCARBAZEPINE")["OXCARBAZEPINE"] == "as_is"


def test_chinese_name_is_never_cut():
    """中文名里的「肠溶片 / 注射液」是名称的一部分，不能按英文剂型截断。"""
    assert _values("阿司匹林肠溶片") == ["阿司匹林肠溶片"]
    assert _values("注射用头孢曲松钠") == ["注射用头孢曲松钠"]


def test_placeholder_yields_no_candidate():
    """安慰剂/盐水不是药品实体，不该送权威库。"""
    for raw in ("Placebo", "placebo comparator", "Saline", "安慰剂", "生理盐水"):
        assert generate_candidates(raw) == [], raw
        assert is_placeholder(raw) is True


def test_placeholder_is_detected_after_normalization():
    assert is_placeholder("  Placebo. ") is True
    assert is_placeholder("Nivolumab") is False


def test_packaging_fragment_is_rejected():
    """'Rx only'、'Single dose vial' 这类残片不是药名。"""
    values = _values("Ifosfamide Injection 3g/60 mL, Single dose vial, Rx only, Sterile")
    assert values == ["Ifosfamide"]


# ══════════════════════ 三、工程约束 ══════════════════════
def test_candidates_are_deduplicated_and_capped():
    raw = ("Little Moon Essentials, Crampy Belly Rub (Camphor 1.1%), Packaged as a) "
           "4 FL OZ (118ML) glass jar, UPC Code 6 73673 88260 2, NDC 70722-260-04")
    values = _values(raw)
    assert len(values) <= MAX_CANDIDATES_PER_NAME
    assert len(values) == len(set(values))


def test_empty_input_returns_no_candidate():
    assert generate_candidates("") == []
    assert generate_candidates("   ") == []
    assert generate_candidates(";") == []


def test_pick_primary_falls_back_to_raw_when_no_candidate():
    """没有候选时也不能丢值：pick_primary 必须返回清洗后的原文。"""
    assert pick_primary("Placebo") == "Placebo"
    assert pick_primary("  Docetaxel  ") == "Docetaxel"


def test_single_token_comma_segment_is_rejected():
    """单 token 逗号片段多为地名/企业名，纳入候选会造成 PubChem 假命中。

    实测教训：'Trimix ... , Pharm D Solutions, Houston, Texas --- NDC ...' 中
    'Texas' 被 PubChem 名称检索模糊匹配并返回了 CID，属典型假阳性。
    """
    raw = ("Trimix QM10 Papaverine 30 mg/Phentolamine 4 mg/Atropine 1 mg/PGE 150 mcg/mL "
           "Injectable, Pharm D Solutions, Houston, Texas --- NDC 69699-1352-05")
    values = _values(raw)
    assert "Texas" not in values
    assert "Houston" not in values
    # 多元词片段仍然保留（'Oxymetazoline HCl' 这类是真药名）
    assert "Oxymetazoline HCl" in _values(
        "No Drip Nasal Spray, Oxymetazoline HCl 0.05% Nasal Decongestant, 1 FL Oz")
