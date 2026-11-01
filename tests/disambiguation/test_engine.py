"""disambiguation.engine（消歧引擎）的单元测试。"""
from unittest.mock import patch

from disambiguation.engine import disambiguate, disambiguate_entities
from disambiguation.models import (
    DisambiguationResult,
    CandidateEntity,
    STATUS_AUTO_MATCHED,
    STATUS_CANDIDATE_MATCHED,
    STATUS_NEW_ENTITY,
)


def _entity(name="阿斯利康", etype="Company"):
    return {
        "entity_name": name,
        "entity_type": etype,
        "entity_data": {},
        "source_text": "",
        "source_site": "nmpa",
    }


class TestDisambiguateEntities:
    def test_step2_alias_keeps_dict_id_as_proposal(self):
        # Weak aliases preserve the selected candidate without authorizing a link.
        result = DisambiguationResult(
            status=STATUS_AUTO_MATCHED,
            dict_id=42,
            confidence=0.95,
            match_step="step2_exact_alias",
            reason="alias exact match",
        )
        with patch("disambiguation.engine.disambiguate", return_value=result):
            entities = disambiguate_entities([_entity()])
        assert entities[0]["dict_id"] is None
        assert entities[0]["_disambig"]["dict_id"] == 42
        assert entities[0]["confidence_score"] == 0.95

    def test_step1_strong_id_sets_dict_id(self):
        # Step1 强 ID 匹配（确定性唯一 ID）也落锤 dict_id
        result = DisambiguationResult(
            status=STATUS_AUTO_MATCHED,
            dict_id=7,
            confidence=0.99,
            match_step="step1_strong_id",
            auto_link=True,
            reason="external id matched",
            candidates=[CandidateEntity(dict_id=7, name="阿斯利康", match_type="external_id")],
        )
        with patch("disambiguation.engine.disambiguate", return_value=result):
            entities = disambiguate_entities([_entity()])
        assert entities[0]["dict_id"] == 7
        assert entities[0]["confidence_score"] == 0.99
        assert entities[0]["_disambig"]["match_step"] == "step1_strong_id"

    def test_step5_reranker_does_not_set_dict_id(self):
        # Reranker scores remain proposals, even for a preferred candidate.
        result = DisambiguationResult(
            status=STATUS_CANDIDATE_MATCHED,
            dict_id=7,
            confidence=0.9,
            match_step="step5_reranker",
            reason="reranker top1",
            candidates=[CandidateEntity(dict_id=7, name="候选", match_type="reranker")],
        )
        with patch("disambiguation.engine.disambiguate", return_value=result):
            entities = disambiguate_entities([_entity()])
        assert entities[0]["dict_id"] is None
        # 但结果仍记录在 _disambig 供前端展示
        assert entities[0]["_disambig"]["status"] == STATUS_CANDIDATE_MATCHED
        assert entities[0]["_disambig"]["match_step"] == "step5_reranker"
        assert entities[0]["_disambig"]["candidates"][0]["dict_id"] == 7

    def test_empty_name_skipped(self):
        entities = [_entity(name="", etype="")]
        with patch("disambiguation.engine.disambiguate") as mock:
            disambiguate_entities(entities)
        mock.assert_not_called()

    def test_error_handled(self):
        with patch("disambiguation.engine.disambiguate", side_effect=RuntimeError("boom")):
            entities = disambiguate_entities([_entity()])
        assert entities[0]["dict_id"] is None
        assert entities[0]["confidence_score"] == 0.0
        assert "disambiguation error" in entities[0]["confidence_reason"]

    def test_candidates_serialized(self):
        result = DisambiguationResult(
            status="ambiguous",
            confidence=0.6,
            match_step="step5_reranker",
            candidates=[
                CandidateEntity(dict_id=1, name="A", similarity_score=0.6, match_type="reranker"),
                CandidateEntity(dict_id=2, name="B", similarity_score=0.4, match_type="reranker"),
            ],
        )
        with patch("disambiguation.engine.disambiguate", return_value=result):
            entities = disambiguate_entities([_entity()])
        cands = entities[0]["_disambig"]["candidates"]
        assert len(cands) == 2
        assert cands[0]["dict_id"] == 1
        assert cands[0]["similarity_score"] == 0.6
        assert cands[0]["match_type"] == "reranker"


class TestDisambiguate:
    def test_step1_strong_id_hit(self):
        result = DisambiguationResult(
            status=STATUS_AUTO_MATCHED, dict_id=42, confidence=0.99,
            reason="external id matched", match_step="step1_strong_id",
        )
        with patch("disambiguation.engine.match_strong_id", return_value=result), \
             patch("disambiguation.engine.lookup_alias_candidates") as mock_lookup:
            r = disambiguate("阿斯利康", "Company", {})
        assert r.dict_id == 42
        assert r.match_step == "step1_strong_id"
        mock_lookup.assert_not_called()  # Step1 命中后不再走 Step2

    def test_step2_alias_original_confidence(self):
        # 第一个候选名命中 → 0.95 / exact_original
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias_candidates", return_value=[{"dict_id": 100}]):
            r = disambiguate("阿斯利康", "Company", {})
        assert r.dict_id == 100
        assert r.confidence == 0.95
        assert r.match_step == "step2_exact_alias"
        assert "exact_original" in r.reason

    def test_step2_alias_middle_confidence(self):
        # 第二个候选名（idx=1）命中 → 0.90 / exact_alias
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias_candidates", side_effect=[[], [{"dict_id": 200}]]):
            r = disambiguate("阿斯利康", "Company", {"name_en": "AZ"})
        assert r.dict_id == 200
        assert r.confidence == 0.90
        assert "exact_alias" in r.reason

    def test_step2_alias_normalized_confidence(self):
        # 第 6 个候选名（idx=5）命中 → 0.85 / exact_normalized
        data = {"name_en": "AZ", "aliases": ["a", "b", "c", "d"]}
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias_candidates",
                   side_effect=[[], [], [], [], [], [{"dict_id": 300}]]):
            r = disambiguate("阿斯利康", "Company", data)
        assert r.dict_id == 300
        assert r.confidence == 0.85
        assert "exact_normalized" in r.reason

    def test_step3_rule_hit(self):
        result = DisambiguationResult(
            status=STATUS_AUTO_MATCHED, dict_id=7, confidence=0.95,
            match_step="step3_drug_cas", reason="drug CAS",
        )
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias_candidates", return_value=[]), \
             patch("disambiguation.engine.match_by_entity_rules", return_value=result):
            r = disambiguate("阿司匹林", "Drug", {"cas_number": "50-78-2"})
        assert r.dict_id == 7
        assert r.match_step == "step3_drug_cas"

    def test_step3_candidate_fallback(self):
        # Step3 返回 candidate_matched（暂存），Step4 无候选 → fallback 返回 step3 结果
        step3 = DisambiguationResult(
            status=STATUS_CANDIDATE_MATCHED, dict_id=9, confidence=0.85,
            match_step="step3_target_rule", reason="target gene",
        )
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias_candidates", return_value=[]), \
             patch("disambiguation.engine.match_by_entity_rules", return_value=step3), \
             patch("disambiguation.engine.generate_embedding", return_value=None), \
             patch("disambiguation.engine.rank_with_reranker") as mock_reranker:
            r = disambiguate("CLDN18.2", "Target", {"gene_symbol": "CLDN18"})
        assert r.status == STATUS_CANDIDATE_MATCHED
        assert r.match_step == "step3_target_rule"
        mock_reranker.assert_not_called()

    def test_step4_step5_pipeline(self):
        reranker_result = DisambiguationResult(
            status=STATUS_CANDIDATE_MATCHED, dict_id=3, confidence=0.9,
            match_step="step5_reranker", reason="reranker top1",
        )
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias_candidates", return_value=[]), \
             patch("disambiguation.engine.match_by_entity_rules", return_value=None), \
             patch("disambiguation.engine.generate_embedding", return_value=[0.1] * 1024), \
             patch("disambiguation.engine.search_similar_entities", return_value=[
                 {"dict_id": 3, "name": "候选", "similarity_score": 0.8,
                  "canonical_data": {}, "aliases": []},
             ]), \
             patch("disambiguation.engine.rank_with_reranker", return_value=reranker_result):
            r = disambiguate("X", "Drug", {})
        assert r.dict_id == 3
        assert r.match_step == "step5_reranker"

    def test_no_match_new_entity(self):
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias_candidates", return_value=[]), \
             patch("disambiguation.engine.match_by_entity_rules", return_value=None), \
             patch("disambiguation.engine.generate_embedding", return_value=[0.1]), \
             patch("disambiguation.engine.search_similar_entities", return_value=[]):
            r = disambiguate("X", "Drug", {})
        assert r.status == STATUS_NEW_ENTITY
        assert r.match_step == "step_none"


class TestNormalizedAlignment:
    """Step0.5 对齐规范化（normalized_name）接入行为：写入 + 进 Step4 向量召回 + 不进 Step2。"""

    def test_normalized_name_written_to_entity_data(self):
        data = {"name_en": ""}
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias", return_value=None), \
             patch("disambiguation.engine.match_by_entity_rules", return_value=None), \
             patch("disambiguation.engine.generate_embedding", return_value=None):
            disambiguate("盐酸二甲双胍片", "Drug", data)
        assert data["normalized_name"] == "二甲双胍"

    def test_normalized_name_used_for_vector_recall(self):
        # normalized_name 作为 vector_normalized 键进 Step4（generate_embedding 收到剥离后核心名）
        emb_calls = []

        def fake_embedding(name):
            emb_calls.append(name)
            return None

        data = {"name_en": ""}
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias", return_value=None), \
             patch("disambiguation.engine.match_by_entity_rules", return_value=None), \
             patch("disambiguation.engine.generate_embedding", side_effect=fake_embedding):
            disambiguate("盐酸二甲双胍片", "Drug", data)
        assert "盐酸二甲双胍片" in emb_calls
        assert "二甲双胍" in emb_calls  # 剥离后核心名参与向量召回

    def test_normalized_name_not_in_step2(self):
        # 过剥零容忍：normalized_name（剥离后）不进 Step2 精确匹配
        data = {"name_en": ""}
        with patch("disambiguation.engine.match_strong_id", return_value=None), \
             patch("disambiguation.engine.lookup_alias", return_value=None) as mock_lookup, \
             patch("disambiguation.engine.match_by_entity_rules", return_value=None), \
             patch("disambiguation.engine.generate_embedding", return_value=None):
            disambiguate("盐酸二甲双胍片", "Drug", data)
        called_names = [c.args[0] for c in mock_lookup.call_args_list]
        assert "二甲双胍" not in called_names
