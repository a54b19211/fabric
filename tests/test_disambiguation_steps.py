"""disambiguate: which step decides, with every collaborator replaced by a scripted stand-in."""

import pytest

from graph.disambiguation import engine
from graph.disambiguation.llm_ranker import SCORE_MID
from graph.disambiguation.models import (
    STATUS_AMBIGUOUS,
    STATUS_AUTO_MATCHED,
    STATUS_CANDIDATE_MATCHED,
    STATUS_NEW_ENTITY,
    CandidateEntity,
    DisambiguationResult,
)


def cand(dict_id, score=0.5, match_type="rule", name=None):
    return CandidateEntity(dict_id=dict_id, name=name or f"N{dict_id}", similarity_score=score, match_type=match_type)


class Vec(list):
    name = ""


class World:
    """The scripted answers of the steps; every call is recorded."""

    def __init__(self):
        self.strong = None
        self.aliases = {}          # candidate name -> [match dict]
        self.rules = None
        self.embedding = {}        # name -> vector | None | Exception
        self.similar = {}          # name -> list of candidate dicts | Exception
        self.reranker = DisambiguationResult(status=STATUS_AMBIGUOUS, confidence=0.4, match_step="step5_reranker")
        self.llm = DisambiguationResult(status=STATUS_AMBIGUOUS, confidence=0.9, match_step="step6_llm")
        self.calls = []


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(engine, "generate_candidate_names", lambda name, kind, data: [name, name + " alias", "a2", "a3", "a4", "tail"])
    monkeypatch.setattr(engine, "normalize_for_alignment", lambda name, kind: name.strip().lower().replace(" ", "-"))
    monkeypatch.setattr(engine, "match_strong_id", lambda kind, data, session=None: w.strong)
    monkeypatch.setattr(engine, "lookup_alias_candidates",
                        lambda name, kind, session=None: w.aliases.get(name, []))
    monkeypatch.setattr(engine, "match_by_entity_rules", lambda name, kind, data, session=None: w.rules)

    def embed(name):
        w.calls.append(("embed", name))
        value = w.embedding.get(name, [0.1])
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        tagged = Vec(value)
        tagged.name = name
        return tagged
    monkeypatch.setattr(engine, "generate_embedding", embed)

    def similar(vector, kind, top_k, session=None, strict_errors=False):
        name = vector.name
        w.calls.append(("similar", name))
        value = w.similar.get(name, [])
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(engine, "search_similar_entities", similar)

    def rerank(*args):
        w.calls.append(("rerank", [c.dict_id for c in args[-1]]))
        if isinstance(w.reranker, Exception):
            raise w.reranker
        return w.reranker
    monkeypatch.setattr(engine, "rank_with_reranker", rerank)

    def llm(*args):
        w.calls.append(("llm", [c.dict_id for c in args[-1]]))
        if isinstance(w.llm, Exception):
            raise w.llm
        return w.llm
    monkeypatch.setattr(engine, "rank_with_llm", llm)
    return w


def run(**kwargs):
    return engine.disambiguate("Alpha Corp", "Company", {"name_en": "alpha corp en"}, "text", "site", **kwargs)


def hit(dict_id, name="Canon"):
    return {"dict_id": dict_id, "canonical_name": name, "canonical_data": {"k": 1}, "external_ids": {"x": "1"}}


def test_strong_id_decides_first_and_keeps_its_candidates(world):
    world.strong = DisambiguationResult(status=STATUS_AUTO_MATCHED, dict_id=3, auto_link=True, match_step="step1",
                                        matched_name="Z")
    result = run()
    assert result is world.strong and [c.dict_id for c in result.candidates] == [3]
    assert not world.calls


def test_one_alias_hit_is_a_candidate_match_scored_by_which_name_hit(world):
    world.aliases = {"Alpha Corp alias": [hit(5)]}
    result = run()
    assert (result.status, result.dict_id, result.confidence, result.match_step) == (
        STATUS_CANDIDATE_MATCHED, 5, 0.90, "step2_exact_alias")
    assert result.candidates[0].match_type == "exact_alias" and result.candidates[0].aliases == ["Alpha Corp alias"]
    assert result.matched_name == "Canon" and "requires review" in result.reason and not world.calls


def test_exact_original_and_normalized_scores_and_ambiguity_over_several_hits(world):
    world.aliases = {"Alpha Corp": [hit(5), hit(6)], "tail": [hit(5), hit(7)]}
    result = run()
    assert result.status == STATUS_AMBIGUOUS and result.dict_id is None
    assert [(c.dict_id, c.similarity_score, c.match_type) for c in result.candidates] == [
        (5, 0.95, "exact_original"), (6, 0.95, "exact_original"), (7, 0.85, "exact_normalized")]
    assert result.confidence == 0.95


def test_auto_matched_rule_result_stops_before_recall(world):
    world.rules = DisambiguationResult(status=STATUS_AUTO_MATCHED, dict_id=8, auto_link=True, match_step="step3")
    result = run()
    assert result is world.rules and [c.dict_id for c in result.candidates] == [8] and not world.calls


def test_rule_candidate_is_returned_when_recall_finds_nothing(world):
    world.rules = DisambiguationResult(status=STATUS_CANDIDATE_MATCHED, dict_id=8, match_step="step3")
    assert run() is world.rules


def test_recall_asks_for_the_name_english_name_and_normalized_name_in_that_order(world):
    world.embedding = {"Alpha Corp": [1], "alpha corp en": [2], "alpha-corp": [3]}
    world.similar = {"Alpha Corp": [{"dict_id": 1, "name": "A", "similarity_score": 0.9}],
                     "alpha corp en": [{"dict_id": 1, "name": "A", "similarity_score": 0.8},
                                       {"dict_id": 2, "name": "B", "similarity_score": 0.7}],
                     "alpha-corp": [{"dict_id": 3, "name": "C", "similarity_score": 0.6}]}
    run()
    assert [call for call in world.calls if call[0] == "embed"] == [
        ("embed", "Alpha Corp"), ("embed", "alpha corp en"), ("embed", "alpha-corp")]
    rerank_call = next(call for call in world.calls if call[0] == "rerank")
    assert rerank_call[1] == [1, 2, 3]


def test_recall_marks_match_types_and_dedups_candidates(world):
    world.embedding = {"Alpha Corp": [1], "alpha corp en": [2], "alpha-corp": [3]}
    world.similar = {"Alpha Corp": [{"dict_id": 1, "name": "A", "similarity_score": 0.9}],
                     "alpha corp en": [{"dict_id": 2, "name": "B", "similarity_score": 0.7}],
                     "alpha-corp": [{"dict_id": 3, "name": "C", "similarity_score": 0.6}]}
    result = run()
    assert [(c.dict_id, c.match_type) for c in result.candidates] == [
        (1, "vector"), (2, "vector_en"), (3, "vector_normalized")]


def test_nothing_recalled_is_a_new_entity_and_failed_recall_is_ambiguous(world):
    result = run()
    assert (result.status, result.match_step, result.reason) == (STATUS_NEW_ENTITY, "step_none", "no match in any step")
    world.embedding = {"Alpha Corp": None}
    result = run()
    assert (result.status, result.match_step) == (STATUS_AMBIGUOUS, "step4_recall_error")
    world.embedding = {"Alpha Corp": RuntimeError("down")}
    result = run()
    assert (result.status, result.match_step) == (STATUS_AMBIGUOUS, "step4_recall_error")


def recalled(world):
    world.embedding = {"Alpha Corp": [1]}
    world.similar = {"Alpha Corp": [{"dict_id": 1, "name": "A", "similarity_score": 0.9}]}


def test_reranker_result_never_links_or_creates(world):
    recalled(world)
    world.reranker = DisambiguationResult(status=STATUS_AUTO_MATCHED, dict_id=1, auto_link=True, confidence=0.99,
                                          match_step="step5_reranker")
    result = run()
    assert result.status == STATUS_AMBIGUOUS and result.auto_link is False
    assert [c.dict_id for c in result.candidates] == [1]
    world.reranker = DisambiguationResult(status=STATUS_NEW_ENTITY, match_step="step5_reranker")
    assert run().status == STATUS_AMBIGUOUS


def test_reranker_failure_is_an_ambiguous_review_proposal_with_the_recalled_candidates(world):
    recalled(world)
    world.reranker = RuntimeError("boom")
    result = run()
    assert (result.status, result.match_step, result.reason) == (
        STATUS_AMBIGUOUS, "step5_reranker_error", "Reranker failed: boom")
    assert [c.dict_id for c in result.candidates] == [1]


def test_llm_review_is_opt_in_and_needs_a_band_score(world):
    recalled(world)
    world.reranker = DisambiguationResult(status=STATUS_AMBIGUOUS, confidence=SCORE_MID, match_step="step5_reranker",
                                          model_trace=[{"m": "rerank"}])
    assert run().match_step == "step5_reranker"
    assert not [call for call in world.calls if call[0] == "llm"]
    world.llm = DisambiguationResult(status=STATUS_AUTO_MATCHED, dict_id=1, auto_link=True, match_step="step6_llm",
                                     model_trace=[{"m": "llm"}])
    result = run(enable_llm_review=True)
    assert (result.status, result.auto_link, result.match_step) == (STATUS_AMBIGUOUS, False, "step6_llm")
    assert result.model_trace == [{"m": "rerank"}, {"m": "llm"}] and [c.dict_id for c in result.candidates] == [1]
    world.reranker = DisambiguationResult(status=STATUS_AMBIGUOUS, confidence=SCORE_MID - 0.01, match_step="step5_reranker")
    assert run(enable_llm_review=True).match_step == "step5_reranker"


def test_llm_failure_keeps_the_reranker_result_with_a_reason(world):
    recalled(world)
    world.reranker = DisambiguationResult(status=STATUS_AMBIGUOUS, confidence=0.99, match_step="step5_reranker")
    world.llm = RuntimeError("llm down")
    result = run(enable_llm_review=True)
    assert result.match_step == "step6_llm_error" and result.reason == "LLM review failed; candidates retained: llm down"
    assert [c.dict_id for c in result.candidates] == [1]


def test_normalized_name_is_added_to_the_entity_data_only_when_it_differs(world):
    data = {"k": 1}
    engine.disambiguate("Alpha Corp", "Company", data)
    assert data == {"k": 1, "normalized_name": "alpha-corp"}
    data = {"k": 1}
    engine.disambiguate("alpha-corp", "Company", data)
    assert data == {"k": 1}
