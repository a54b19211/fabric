"""Scoped identifier rules use mocked dictionary candidates."""
from unittest.mock import patch

from graph.disambiguation.models import STATUS_AUTO_MATCHED, STATUS_CANDIDATE_MATCHED
from graph.disambiguation.rules import STRONG_ID_FIELDS, match_by_entity_rules, match_strong_id


class TestStrongIdFields:
    def test_cas_only_identifies_compounds(self):
        assert "cas_number" in STRONG_ID_FIELDS["Compound"]
        assert "cas_number" not in STRONG_ID_FIELDS.get("Drug", [])

    def test_gene_symbol_does_not_identify_a_target(self):
        assert "gene_symbol" not in STRONG_ID_FIELDS.get("Target", [])

    def test_unknown_type_absent(self):
        assert "DealItem" not in STRONG_ID_FIELDS

    def test_placeholder_id_removed(self):
        for entity_type in ("Company", "Drug", "Target", "Indication"):
            assert "id" not in STRONG_ID_FIELDS.get(entity_type, [])

    def test_pathway_reactome_id_kept(self):
        assert "id" in STRONG_ID_FIELDS["Pathway"]

    def test_policy_doc_number_removed(self):
        assert "Policy" not in STRONG_ID_FIELDS

    def test_indication_icd_code_removed(self):
        assert "Indication" not in STRONG_ID_FIELDS


class TestMatchStrongId:
    def test_hit(self):
        with patch("graph.disambiguation.rules.lookup_external_id_candidates", return_value=[
            {"dict_id": 42, "canonical_name": "阿司匹林", "external_ids": {"cas_number": "50-78-2"}},
        ]):
            result = match_strong_id("Compound", {"cas_number": "50-78-2"})
        assert result.status == STATUS_AUTO_MATCHED
        assert result.dict_id == 42
        assert result.confidence == 0.99
        assert result.match_step == "step1_strong_id"
        assert result.matched_name == "阿司匹林"
        assert result.auto_link is True

    def test_miss(self):
        with patch("graph.disambiguation.rules.lookup_external_id_candidates", return_value=[]):
            assert match_strong_id("Compound", {"cas_number": "50-78-2"}) is None

    def test_no_entity_data(self):
        assert match_strong_id("Drug", None) is None

    def test_no_strong_id_field(self):
        assert match_strong_id("DealItem", {"name": "x"}) is None

    def test_empty_value_skipped(self):
        assert match_strong_id("Compound", {"cas_number": "   "}) is None


class TestMatchByEntityRules:
    def test_company_rule(self):
        with patch("graph.disambiguation.rules.lookup_external_id_candidates", return_value=[
            {"dict_id": 7, "canonical_name": "阿斯利康", "external_ids": {"exchange": "NASDAQ"}},
        ]):
            result = match_by_entity_rules("阿斯利康", "Company",
                                           {"stock_code": "AZN", "exchange": "NASDAQ"})
        assert result.status == STATUS_AUTO_MATCHED
        assert result.dict_id == 7
        assert result.auto_link is True

    def test_drug_cas_rule(self):
        with patch("graph.disambiguation.rules.lookup_external_id_candidates", return_value=[
            {"dict_id": 8, "canonical_name": "阿司匹林"},
        ]):
            result = match_by_entity_rules("阿司匹林", "Drug", {"cas_number": "50-78-2"})
        assert result.status == STATUS_CANDIDATE_MATCHED
        assert result.auto_link is False

    def test_target_gene_rule(self):
        with patch("graph.disambiguation.rules.lookup_external_id_candidates", return_value=[
            {"dict_id": 9, "canonical_name": "CLDN18"},
        ]):
            result = match_by_entity_rules("CLDN18.2", "Target", {"gene_symbol": "CLDN18"})
        assert result.status == STATUS_CANDIDATE_MATCHED
        assert result.dict_id == result.candidates[0].dict_id == 9

    def test_no_entity_data(self):
        assert match_by_entity_rules("x", "Drug", None) is None

    def test_unknown_type(self):
        assert match_by_entity_rules("x", "Foo", {"name": "x"}) is None

    def test_missing_required_field(self):
        assert match_by_entity_rules("x", "Company", {"name": "x"}) is None
