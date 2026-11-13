"""API 字段映射（field_map 候选链）与 fda 药名/批准文号通路测试。

背景（A0.6，2026-10-27）：
    `fda_api_enforcement` 的 `drug_name` 此前单映射到 `product_description`，
    落库值是整段「剂型 + 包装 + 企业 + 地址 + NDC」描述，不是药名。而 openFDA
    的原始记录里其实带 `openfda` 子对象，含 generic_name / brand_name /
    substance_name / application_number 四个规范字段 —— 只是覆盖率不均匀
    （实测 10 条召回记录仅 3 条非空），单路径映射无法「有则用、无则退」。

    修法是给 field_map 增加「候选链」语义：按顺序取值、返回第一个非空，
    配置里表达「结构化优先、原文兜底」，而不是在代码里写死站点特例。

本文件锁定的行为：
    1. 候选链按顺序取第一个非空值；全空返回 ""（不抛异常）
    2. 单路径映射保持原语义（回归保护）
    3. fda_api_enforcement：openfda 有值时取规范名，无值时整段描述兜底
       —— 兜底时**不截断**
    4. fda 的 approval_number 能从 openfda.application_number.0 取到，
       并能在 validate_extra 后保留（否则 T3 标识符对齐在 fda 上是 0 输入）
"""


from fetch.api_fields import _extract_extra_value, _get_nested
from fetch.sites.fda.api_drug_label import CONFIG as LABEL_CFG
from fetch.sites.fda.api_enforcement import CONFIG as ENFORCEMENT_CFG
from graph.db.schemas import EXTRA_MODEL_MAP, validate_extra

# 真实 openFDA 召回记录（2026-09-20 采集，uuid 0fa81856）
RECORD_WITH_OPENFDA = {
    "product_description": (
        "VIRACEPT (R) (nelfinavir mesylate) Tablets, 625 mg, 120-count bottle, "
        "Rx only, Manufactured by Agouron Pharmaceuticals, NDC 63010-010-12"
    ),
    "recall_number": "D-1066-2013",
    "classification": "Class II",
    "report_date": "20131009",
    "openfda": {
        "application_number": ["NDA020779"],
        "brand_name": ["VIRACEPT"],
        "generic_name": ["NELFINAVIR MESYLATE"],
        "substance_name": ["NELFINAVIR MESYLATE"],
        "product_ndc": ["63010-010", "63010-027"],
    },
}

# 同站点但 openfda 为空的记录（实测 7/10 属此类）
RECORD_WITHOUT_OPENFDA = {
    "product_description": (
        "Ifosfamide Injection 3g/60 mL, Single dose vial, Rx only, Sterile, "
        "For Intravenous Use, NDC 0069-4496-22"
    ),
    "recall_number": "D-1066-2014",
    "classification": "Class II",
    "openfda": {},
}


# ── 候选链基础语义 ────────────────────────────────────────────────────


def test_get_nested_single_path_unchanged():
    """单路径映射保持原语义（老配置不能因为加了候选链就变）。"""
    assert _get_nested(RECORD_WITH_OPENFDA, "recall_number") == "D-1066-2013"
    assert _get_nested(RECORD_WITH_OPENFDA, "openfda.brand_name.0") == "VIRACEPT"
    assert _get_nested(RECORD_WITH_OPENFDA, "not.exist.path") == ""


def test_get_nested_candidate_chain_returns_first_non_empty():
    """候选链按顺序返回第一个非空值，不拼接、不报错。"""
    chain = ["openfda.generic_name.0", "openfda.brand_name.0", "product_description"]
    assert _get_nested(RECORD_WITH_OPENFDA, chain) == "NELFINAVIR MESYLATE"


def test_get_nested_candidate_chain_falls_back_when_earlier_empty():
    """前序候选全空时落到兜底项。"""
    chain = ["openfda.generic_name.0", "openfda.brand_name.0", "product_description"]
    assert _get_nested(RECORD_WITHOUT_OPENFDA, chain) == RECORD_WITHOUT_OPENFDA[
        "product_description"
    ]


def test_get_nested_candidate_chain_all_empty_returns_blank():
    """全空返回空串而不是抛异常（映射层不允许中断整条记录）。"""
    assert _get_nested(RECORD_WITHOUT_OPENFDA, ["openfda.brand_name.0", "missing"]) == ""


def test_get_nested_candidate_chain_supports_const():
    """候选链里混入 __const__ 时同样生效。"""
    assert _get_nested({}, ["missing.path", "__const__:药品召回"]) == "药品召回"


# ── fda 站点配置：药名 ────────────────────────────────────────────────


def test_enforcement_drug_name_prefers_structured_name():
    """有 openfda 时，药名应是规范通用名，不是整段产品描述。"""
    spec = ENFORCEMENT_CFG["field_map"]["drug_name"]
    got = _get_nested(RECORD_WITH_OPENFDA, spec)
    assert got == "NELFINAVIR MESYLATE"
    assert got != RECORD_WITH_OPENFDA["product_description"]


def test_enforcement_drug_name_falls_back_without_truncation():
    """openfda 为空时整段描述兜底，且**不在此处截断**。

    映射层先截断，后面就看不到原文了。
    """
    spec = ENFORCEMENT_CFG["field_map"]["drug_name"]
    got = _get_nested(RECORD_WITHOUT_OPENFDA, spec)
    assert got == RECORD_WITHOUT_OPENFDA["product_description"]
    assert got.count(",") == RECORD_WITHOUT_OPENFDA["product_description"].count(",")


def test_label_drug_name_chain_order():
    """说明书类：通用名 → 品牌名 → 物质名，三级回退。"""
    spec = LABEL_CFG["field_map"]["drug_name"]
    assert _get_nested({"openfda": {"brand_name": ["X"]}}, spec) == "X"
    assert _get_nested({"openfda": {"substance_name": ["Y"]}}, spec) == "Y"


# 真实记录（openFDA 药品说明书，set_id ed66d635-…，2010 年）：openfda 子对象是空的，没有任何药名，
# spl_product_data_elements 的第一项是“产品名 + 成分名”一整串（这里截短）。早期按生效时间升序读，最先拿到的就是这类记录。
OLD_LABEL_WITHOUT_OPENFDA = {
    "set_id": "ed66d635-4b72-4292-b64b-744c6dbe24d7",
    "effective_time": "201001004",
    "openfda": {},
    "spl_product_data_elements": ["CVS Antibacterial Foaming Kitchen Triclosan TRICLOSAN TRICLOSAN WATER"],
    "package_label_principal_display_panel": ["Package Front and Back Labels cvs8.jpg 7.5 OZ Front and Back Labels"],
}


def test_label_title_chain_prefers_brand_name_and_never_ends_empty():
    """说明书标题以前只映射 openfda.brand_name.0，openfda 为空的记录标题就是空串。
    候选链：品牌名 → 通用名 → 物质名 → SPL 产品清单第一项。"""
    spec = LABEL_CFG["field_map"]["title"]
    with_names = {"openfda": {"brand_name": ["VIRACEPT"], "generic_name": ["NELFINAVIR"]},
                  "spl_product_data_elements": ["VIRACEPT nelfinavir mesylate"]}
    assert _get_nested(with_names, spec) == "VIRACEPT"
    assert _get_nested({"openfda": {"generic_name": ["NELFINAVIR"]}}, spec) == "NELFINAVIR"
    assert _get_nested(OLD_LABEL_WITHOUT_OPENFDA, spec).startswith("CVS Antibacterial Foaming Kitchen Triclosan")
    # 并且新的在前：升序最先读到的 2006–2010 年老说明书，前 100 条只有 2 条有品牌名；降序前 100 条有 95 条
    assert LABEL_CFG["api"]["sort"] == "effective_time:desc"


# ── fda 站点配置：批准文号 ────────────────────────────────────────────


def test_enforcement_approval_number_extracted_from_openfda():
    """approval_number 从 openfda.application_number.0 取值。

    补这条的原因：fda 此前没有任何可校验的标识符字段，
    T3 标识符对齐在这两个站点上是 0 输入。
    """
    spec = ENFORCEMENT_CFG["extra_fields"]["approval_number"]
    assert _extract_extra_value(RECORD_WITH_OPENFDA, spec) == "NDA020779"
    assert _extract_extra_value(RECORD_WITHOUT_OPENFDA, spec) == ""


def test_enforcement_extra_keeps_approval_number_after_validation():
    """validate_extra 不能把新字段当未知字段丢掉。"""
    raw = _extract_extra_value(
        RECORD_WITH_OPENFDA, ENFORCEMENT_CFG["extra_fields"]["approval_number"]
    )
    out = validate_extra("fda_api_enforcement", {"drug_name": "VIRACEPT", "approval_number": raw})
    assert out["approval_number"] == "NDA020779"
    assert out["drug_name"] == "VIRACEPT"


def test_label_extra_model_registered():
    """fda_api_drug_label 之前不在 EXTRA_MODEL_MAP，extra 完全不受校验。"""
    assert "fda_api_drug_label" in EXTRA_MODEL_MAP
    out = validate_extra(
        "fda_api_drug_label",
        {"approval_number": "ANDA217782", "product_ndc": ["85742-007"], "spl_set_id": "x"},
    )
    assert out == {
        "drug_name": "",
        "approval_number": "ANDA217782",
        "product_ndc": ["85742-007"],
        "spl_set_id": "x",
    }


