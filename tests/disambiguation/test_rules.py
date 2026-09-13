"""disambiguation.rules（强 ID + 类型规则匹配）的单元测试。"""
from unittest.mock import patch

from disambiguation.rules import (
    STRONG_ID_FIELDS,
    match_strong_id,
    match_by_entity_rules,
)
from disambiguation.models import (
    STATUS_AUTO_MATCHED,
    STATUS_CANDIDATE_MATCHED,
)


class TestStrongIdFields:
    def test_drug_has_cas(self):
        assert "cas_number" in STRONG_ID_FIELDS["Drug"]

    def test_target_has_gene_symbol(self):
        assert "gene_symbol" in STRONG_ID_FIELDS["Target"]

    def test_unknown_type_absent(self):
        assert "DealItem" not in STRONG_ID_FIELDS


class TestMatchStrongId:
    def test_hit(self):
        with patch(
            "disambiguation.rules.lookup_external_id",
            return_value={"dict_id": 42, "canonical_name": "阿司匹林"},
        ):
            result = match_strong_id("Drug", {"cas_number": "50-78-2"})
        assert result is not None
        assert result.status == STATUS_AUTO_MATCHED
        assert result.dict_id == 42
        assert result.confidence == 0.99
        assert result.match_step == "step1_strong_id"
        assert result.matched_name == "阿司匹林"

    def test_miss(self):
        with patch("disambiguation.rules.lookup_external_id", return_value=None):
            result = match_strong_id("Drug", {"cas_number": "50-78-2"})
        assert result is None

    def test_no_entity_data(self):
        assert match_strong_id("Drug", None) is None

    def test_no_strong_id_field(self):
        # DealItem 不在 STRONG_ID_FIELDS 中，应直接返回 None 且不调用 lookup
        assert match_strong_id("DealItem", {"name": "x"}) is None

    def test_empty_value_skipped(self):
        result = match_strong_id("Drug", {"cas_number": "   "})
        assert result is None


class TestMatchByEntityRules:
    def test_company_rule(self):
        with patch(
            "disambiguation.rules.lookup_external_id",
            return_value={"dict_id": 7, "canonical_name": "阿斯利康"},
        ):
            result = match_by_entity_rules(
                "阿斯利康", "Company",
                {"stock_code": "AZN", "exchange": "NASDAQ"},
            )
        assert result is not None
        assert result.status == STATUS_AUTO_MATCHED
        assert result.dict_id == 7
        assert result.confidence == 0.95

    def test_drug_cas_rule(self):
        with patch(
            "disambiguation.rules.lookup_external_id",
            return_value={"dict_id": 8, "canonical_name": "阿司匹林"},
        ):
            result = match_by_entity_rules("阿司匹林", "Drug", {"cas_number": "50-78-2"})
        assert result is not None
        assert result.status == STATUS_AUTO_MATCHED
        assert result.confidence == 0.95

    def test_target_gene_rule(self):
        with patch(
            "disambiguation.rules.lookup_external_id",
            return_value={"dict_id": 9, "canonical_name": "CLDN18"},
        ):
            result = match_by_entity_rules(
                "CLDN18.2", "Target", {"gene_symbol": "CLDN18"},
            )
        assert result is not None
        assert result.status == STATUS_CANDIDATE_MATCHED
        assert result.confidence == 0.85

    def test_no_entity_data(self):
        assert match_by_entity_rules("x", "Drug", None) is None

    def test_unknown_type(self):
        assert match_by_entity_rules("x", "Foo", {"name": "x"}) is None

    def test_missing_required_field(self):
        # Company 无 stock_code，应返回 None
        assert match_by_entity_rules("x", "Company", {"name": "x"}) is None
