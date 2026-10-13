"""
字段标准化 L3（语义层归一）单元测试。

锁定三类行为：
1. 同义词归一：别名 / 商品名 → 标准通用名
2. 单位换算：浓度 / 剂量单位标量归一（质量→g，体积→L）
3. 未命中兜底：映射表无匹配时返回原值（绝不丢数据）

对应开题报告 §R19「前期探索性原型暴露的不足」：
- 同物异名无归一 → 现通过 _SYNONYM_MAP 收敛为通用名
- 单位未统一 → 现通过 _UNIT_MAP 标量换算
"""

import pytest

from biz_rules.cleaners.normalizer_l3 import (
    normalize_l3,
    normalize_synonym,
    normalize_unit_expression,
)


# ───────────────────────── 1. 同义词归一 ─────────────────────────
class TestSynonymNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("aspirin", "阿司匹林"),
            ("Aspirin", "阿司匹林"),
            ("乙酰水杨酸", "阿司匹林"),
            ("acetylsalicylic acid", "阿司匹林"),
            ("扑热息痛", "对乙酰氨基酚"),
            ("paracetamol", "对乙酰氨基酚"),
            ("ceftriaxone", "头孢曲松"),
        ],
    )
    def test_synonym_hit(self, raw, expected):
        assert normalize_synonym(raw) == expected

    def test_synonym_no_match_returns_original(self):
        # 映射表无该词条 → 原样返回，不丢数据
        assert normalize_synonym("某未收录新药") == "某未收录新药"

    def test_synonym_empty_returns_empty(self):
        assert normalize_synonym("") == ""
        assert normalize_synonym(None) is None


# ───────────────────────── 2. 单位换算 ─────────────────────────
class TestUnitConversion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("500 mg", "0.5 g"),
            ("100μg", "0.0001 g"),
            ("100 ug", "0.0001 g"),
            ("2 g", "2 g"),
            ("0.5 L", "0.5 L"),
            ("1000 ml", "1 L"),
        ],
    )
    def test_unit_conversion(self, raw, expected):
        assert normalize_unit_expression(raw) == expected

    def test_unit_no_number_returns_original(self):
        # 纯字符串，无法解析 → 原值
        assert normalize_unit_expression("mg") == "mg"

    def test_unit_unknown_unit_returns_original(self):
        # 单位不在表内 → 原值
        assert normalize_unit_expression("5 mol") == "5 mol"

    def test_unit_empty_returns_empty(self):
        assert normalize_unit_expression("") == ""


# ──────────────────── 3. 统一入口 normalize_l3 ────────────────────
class TestNormalizeL3:
    def test_drug_name_routes_to_synonym(self):
        assert normalize_l3("aspirin", field_type="drug_name") == "阿司匹林"

    def test_dosage_routes_to_unit(self):
        assert normalize_l3("500 mg", field_type="dosage") == "0.5 g"

    def test_chinese_field_type_routes_to_unit(self):
        assert normalize_l3("100μg", field_type="浓度") == "0.0001 g"

    def test_unknown_drug_no_match_returns_original(self):
        # 默认走同义词路径，未命中 → 原值
        assert normalize_l3("某未收录新药", field_type="drug_name") == "某未收录新药"

    def test_empty_value_returns_original(self):
        assert normalize_l3("", field_type="drug_name") == ""
        assert normalize_l3(None, field_type="dosage") is None

    def test_default_field_type_is_drug_name(self):
        # field_type 缺省 → 同义词归一
        assert normalize_l3("paracetamol") == "对乙酰氨基酚"
