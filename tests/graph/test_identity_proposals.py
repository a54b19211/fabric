"""Identity safety regressions; all repositories and models are mocked."""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from graph.disambiguation.engine import disambiguate, disambiguate_entities
from graph.disambiguation.llm_ranker import rank_with_llm, rank_with_reranker
from graph.disambiguation.models import CandidateEntity, DisambiguationResult
from graph.disambiguation.rules import match_by_entity_rules, match_strong_id


def _row(dict_id=7, **data):
    return {"dict_id": dict_id, "canonical_name": "Known", "canonical_data": data,
            "external_ids": data}


def _lookups(rows):
    stack = ExitStack()
    stack.enter_context(patch("graph.disambiguation.rules.lookup_external_id",
                              return_value=rows[0] if rows else None, create=True))
    stack.enter_context(patch("graph.disambiguation.rules.lookup_external_id_candidates",
                              return_value=rows, create=True))
    return stack


@pytest.mark.parametrize("entity_type,data,rows", [
    ("Target", {"gene_symbol": "CLDN18"}, [_row(gene_symbol="CLDN18")]),
    ("Company", {"stock_code": "000001"}, [_row(stock_code="000001")]),
    ("Company", {"stock_code": "AZN", "exchange": "NASDAQ"},
     [_row(stock_code="AZN", exchange="LSE")]),
    ("Drug", {"cas_number": "50-78-2"}, [_row(cas_number="50-78-2")]),
    ("ClinicalTrial", {"trial_id": "trial_123"}, [_row(trial_id="trial_123")]),
    ("ClinicalTrial", {"trial_id": "NCT00000001"},
     [_row(7, trial_id="NCT00000001"), _row(8, trial_id="NCT00000001")]),
    ("Compound", {"cas_number": "50-78-9"}, [_row(cas_number="50-78-9")]),
    ("Pathway", {"id": "internal_123"}, [_row(id="internal_123")]),
])
def test_weak_wrong_grain_and_duplicate_ids_never_auto_link(entity_type, data, rows):
    with _lookups(rows):
        result = match_strong_id(entity_type, data)
    assert result is None or result.status != "auto_matched"


@pytest.mark.parametrize("entity_type,data", [
    ("ClinicalTrial", {"trial_id": "NCT00000001"}),
    ("Compound", {"cas_number": "50-78-2"}),
    ("Pathway", {"id": "R-HSA-109581"}),
    ("Company", {"stock_code": "AZN", "exchange": "NASDAQ"}),
])
def test_unique_scoped_ids_have_explicit_link_evidence(entity_type, data):
    with _lookups([_row(**data)]):
        result = match_strong_id(entity_type, data)
    assert result.status == "auto_matched"
    assert result.auto_link is True
    assert result.candidates[0].dict_id == 7
    assert result.rule_version


@pytest.mark.parametrize("stock,canonical_exchange", [("internal_123", "NASDAQ"),
                                                     ("AZN", "LSE")])
def test_company_identifier_requires_valid_symbol_and_consistent_namespace(stock, canonical_exchange):
    row = {"dict_id": 7, "canonical_name": "Known",
           "canonical_data": {"exchange": canonical_exchange},
           "external_ids": {"stock_code": stock, "exchange": "NASDAQ"}}
    with _lookups([row]):
        result = match_strong_id("Company", {"stock_code": stock, "exchange": "NASDAQ"})
    assert result is None or result.status != "auto_matched"


@pytest.mark.parametrize("entity_type,data", [
    ("Drug", {"id": "internal_123"}),
    ("Target", {"gene_symbol": "CLDN18"}),
    ("Company", {"stock_code": "000001"}),
])
def test_type_rules_keep_weak_identifier_candidates(entity_type, data):
    with _lookups([_row(**data)]):
        result = match_by_entity_rules("Known", entity_type, data)
    assert result.status == "candidate_matched"
    assert result.dict_id == 7
    assert [candidate.dict_id for candidate in result.candidates] == [7]


def test_aliases_are_proposals_and_keep_each_matching_name():
    with patch("graph.disambiguation.engine.match_strong_id", return_value=None), \
         patch("graph.disambiguation.engine.generate_candidate_names", return_value=["A", "B"]), \
         patch("graph.disambiguation.engine.lookup_alias_candidates", side_effect=[[{"dict_id": 7}], [{"dict_id": 8}]]):
        result = disambiguate("A", "Company", {})
    assert result.status == "ambiguous"
    assert {c.dict_id for c in result.candidates} == {7, 8}


@pytest.mark.parametrize("step", ["step3_target_rule", "step5_reranker"])
def test_selected_candidate_and_persistable_metadata_survive_batch(step):
    result = DisambiguationResult(status="candidate_matched", dict_id=7,
                                  matched_name="Known", confidence=0.83,
                                  reason="Needs review", match_step=step)
    entity = {"entity_name": "Known", "entity_type": "Target", "entity_data": {}}
    with patch("graph.disambiguation.engine.disambiguate", return_value=result):
        disambiguate_entities([entity])
    assert entity["dict_id"] is None
    proposal = entity["_disambig"]
    assert entity["entity_data"]["_disambig"] == proposal
    assert proposal["dict_id"] == 7
    assert proposal["candidates"][0]["dict_id"] == 7
    assert proposal["confidence"] == 0.83
    assert proposal["reason"] == "Needs review"
    assert proposal["rule_version"]
    assert proposal["model_version"] is None
    assert proposal["decision_policy"] == "strong_ids_only"
    assert proposal["calibration_status"] == "uncalibrated"


def test_old_alias_auto_status_does_not_authorize_a_link():
    entity = {"entity_name": "A", "entity_type": "Company", "entity_data": {}}
    result = DisambiguationResult(status="auto_matched", dict_id=7,
                                  match_step="step2_exact_alias")
    with patch("graph.disambiguation.engine.disambiguate", return_value=result):
        disambiguate_entities([entity])
    assert entity["dict_id"] is None


class _Response:
    def __init__(self, results):
        self.results = results

    def raise_for_status(self):
        pass

    def json(self):
        return {"results": self.results}


@pytest.mark.parametrize("results", [[], [{"index": -1, "relevance_score": 0.99}],
                                    [{"index": 0, "relevance_score": "bad"}]])
def test_reranker_bad_or_empty_output_does_not_declare_a_new_object(results):
    candidates = [CandidateEntity(7, "Known")]
    with patch("graph.disambiguation.llm_ranker.requests.post", return_value=_Response(results)):
        result = rank_with_reranker("A", "Drug", {}, "", candidates)
    assert result.status == "ambiguous"
    assert [c.dict_id for c in result.candidates] == [7]


@pytest.mark.parametrize("output", ['{"match":"candidate_-1","confidence":0.9}',
                                   '{"match":"none","confidence":0.9}',
                                   '{"match":"candidate_0","confidence":"oops"}',
                                   'not json'])
def test_llm_output_is_a_recorded_proposal_and_never_drops_candidates(output):
    candidates = [CandidateEntity(i, f"Candidate {i}") for i in range(1, 8)]
    with patch("graph.disambiguation.llm_ranker.qwen_chat", return_value=output):
        result = rank_with_llm("A", "Drug", {}, "source", candidates)
    assert result.status == "ambiguous"
    assert result.dict_id is None
    assert [c.dict_id for c in result.candidates] == list(range(1, 8))
    assert result.model_trace[-1]["output"] == output
    assert result.model_trace[-1]["input"]["user_message"]


@pytest.mark.parametrize("enable,score,expected_calls", [(False, 0.6, 0), (True, 0.6, 1),
                                                      (True, 0.2, 0)])
def test_step6_requires_explicit_enable_and_review_band(enable, score, expected_calls):
    candidate = CandidateEntity(7, "Known", similarity_score=score)
    ranked = DisambiguationResult(status="ambiguous", confidence=score,
                                  candidates=[candidate], match_step="step5_reranker")
    with patch("graph.disambiguation.engine.match_strong_id", return_value=None), \
         patch("graph.disambiguation.engine.lookup_alias_candidates", return_value=[]), \
         patch("graph.disambiguation.engine.match_by_entity_rules", return_value=None), \
         patch("graph.disambiguation.engine.generate_embedding", return_value=[0.1]), \
         patch("graph.disambiguation.engine.search_similar_entities", return_value=[
             {"dict_id": 7, "name": "Known", "similarity_score": score}]), \
         patch("graph.disambiguation.engine.rank_with_reranker", return_value=ranked), \
         patch("graph.disambiguation.engine.rank_with_llm", return_value=ranked) as llm:
        result = disambiguate("A", "Drug", {}, enable_llm_review=enable)
    assert llm.call_count == expected_calls
    assert result.status == "ambiguous"


def test_llm_success_keeps_unsent_candidates_and_records_exact_model_input():
    candidates = [CandidateEntity(i, f"Candidate {i}") for i in range(1, 8)]
    output = '{"match":"candidate_1","confidence":0.8,"reason":"context agrees"}'
    with patch("graph.disambiguation.llm_ranker.qwen_chat", return_value=output) as model:
        result = rank_with_llm("A", "Drug", {}, "source", candidates)
    assert result.status == "candidate_matched"
    assert result.dict_id == 2
    assert result.auto_link is False
    assert len(result.candidates) == 7
    trace = result.model_trace[-1]
    assert trace["input"]["candidate_ids"] == [1, 2, 3, 4, 5]
    assert trace["input"]["user_message"] == model.call_args.kwargs["user_message"]
    assert trace["output"] == output


def test_llm_exception_is_review_evidence_and_never_a_new_object():
    with patch("graph.disambiguation.llm_ranker.qwen_chat", side_effect=RuntimeError("model unavailable")):
        result = rank_with_llm("A", "Drug", {}, "source", [CandidateEntity(7, "Known")])
    assert result.status == "ambiguous"
    assert result.model_trace[-1]["input"]["user_message"]
    assert "model unavailable" in result.model_trace[-1]["error"]
    assert result.candidates[0].dict_id == 7


def test_step3_selected_id_survives_reranker_output_which_omits_candidates():
    step3 = DisambiguationResult(status="candidate_matched", dict_id=7, matched_name="Rule",
                                 match_step="step3_target_rule")
    ranked = DisambiguationResult(status="candidate_matched", dict_id=8, matched_name="Vector",
                                  confidence=0.9, match_step="step5_reranker")
    with patch("graph.disambiguation.engine.match_strong_id", return_value=None), \
         patch("graph.disambiguation.engine.lookup_alias_candidates", return_value=[]), \
         patch("graph.disambiguation.engine.match_by_entity_rules", return_value=step3), \
         patch("graph.disambiguation.engine.generate_embedding", return_value=[0.1]), \
         patch("graph.disambiguation.engine.search_similar_entities", return_value=[
             {"dict_id": 8, "name": "Vector", "similarity_score": 0.8}]), \
         patch("graph.disambiguation.engine.rank_with_reranker", return_value=ranked):
        result = disambiguate("A", "Target", {})
    assert result.dict_id == 8
    assert {candidate.dict_id for candidate in result.candidates} == {7, 8}


def test_real_repository_similarity_error_cannot_mean_no_candidates():
    # Leave the actual repository helper in use: its exception handler used to
    # turn database failures into [], so an engine-only mock missed the bug.
    session = MagicMock()
    session.execute.side_effect = RuntimeError("synthetic query unavailable")
    session.query.side_effect = RuntimeError("synthetic query unavailable")
    with patch("graph.disambiguation.engine.match_strong_id", return_value=None), \
         patch("graph.disambiguation.engine.lookup_alias_candidates", return_value=[]), \
         patch("graph.disambiguation.engine.match_by_entity_rules", return_value=None), \
         patch("graph.disambiguation.engine.generate_embedding", return_value=[0.1]):
        result = disambiguate("Unknown", "Drug", {}, session=session)
    assert result.status == "ambiguous"
    assert result.match_step == "step4_recall_error"


def test_real_alias_repository_preserves_same_name_multiple_dictionary_ids():
    from types import SimpleNamespace

    session = MagicMock()
    query = session.query.return_value
    query.join.return_value = query
    query.filter.return_value = query
    query.distinct.return_value = query
    query.order_by.return_value = query
    query.all.return_value = [SimpleNamespace(id=7, canonical_data={"name": "A"}, external_ids={}),
                              SimpleNamespace(id=8, canonical_data={"name": "B"}, external_ids={})]
    with patch("graph.disambiguation.engine.match_strong_id", return_value=None), \
         patch("graph.disambiguation.engine.generate_candidate_names", return_value=["Shared"]):
        result = disambiguate("Shared", "Company", {}, session=session)
    assert result.status == "ambiguous"
    assert result.dict_id is None
    assert {candidate.dict_id for candidate in result.candidates} == {7, 8}
