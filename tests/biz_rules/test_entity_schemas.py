"""biz_rules.entity_schemas 的单元测试。"""
from biz_rules.entity_schemas import (
    ENTITY_SCHEMA_MAP,
    VALID_ENTITY_TYPES,
    _sanitize_entity_data,
    validate_entity_data,
)


class TestValidateEntityData:
    def test_known_type_drug_defaults_filled(self):
        result = validate_entity_data("Drug", {"name": "阿司匹林"})
        assert result["name"] == "阿司匹林"
        assert result["aliases"] == []       # 默认字段填充
        assert result["phase_cn"] == ""      # 默认字段填充

    def test_known_type_company_type_conversion(self):
        # pydantic 应把字符串年份转成 int
        result = validate_entity_data("Company", {"name": "X", "founded_year": "2010"})
        assert result["founded_year"] == 2010

    def test_aliases_none_sanitized(self):
        # aliases=None 触发 pydantic 校验失败 → sanitize 兜底为 []
        result = validate_entity_data("Drug", {"aliases": None})
        assert result["aliases"] == []

    def test_unknown_type_passthrough(self):
        result = validate_entity_data("Foo", {"x": 1})
        assert result == {"x": 1}

    def test_unknown_type_aliases_none(self):
        result = validate_entity_data("Foo", {"aliases": None, "x": 1})
        assert result["aliases"] == []
        assert result["x"] == 1


class TestSanitizeEntityData:
    def test_aliases_none_to_empty(self):
        d = {"aliases": None}
        result = _sanitize_entity_data(d)
        assert result["aliases"] == []
        assert d["aliases"] == []  # 原地修改

    def test_aliases_kept(self):
        d = {"aliases": ["A"]}
        result = _sanitize_entity_data(d)
        assert result["aliases"] == ["A"]


class TestEntityTypeRegistry:
    def test_native_types_preserve_registration_and_regulatory_grain(self):
        assert {"Organization", "Intervention", "RegulatoryApplication", "RecallProduct"} <= VALID_ENTITY_TYPES
        application = validate_entity_data("RegulatoryApplication", {"application_number": "NDA209510"})
        product = validate_entity_data("RecallProduct", {"recall_number": "D-1234-2026", "event_id": "999"})
        assert application["application_number"] == "NDA209510"
        assert product["recall_number"] == "D-1234-2026" and product["event_id"] == "999"

    def test_common_types_present(self):
        for t in ("Company", "Drug", "Target", "Indication", "ClinicalTrial"):
            assert t in ENTITY_SCHEMA_MAP
