"""disambiguation.llm_ranker（Step5 Reranker 精排）的单元测试。"""
from unittest.mock import patch

from disambiguation.llm_ranker import (
    rank_with_reranker,
    _build_rich_name,
    _build_rich_text,
)
from disambiguation.models import (
    CandidateEntity,
    STATUS_CANDIDATE_MATCHED,
    STATUS_NEW_ENTITY,
    STATUS_AMBIGUOUS,
)


def _make_candidates(*names):
    return [CandidateEntity(dict_id=i + 1, name=n) for i, n in enumerate(names)]


class _FakeResponse:
    def __init__(self, results):
        self._results = results

    def raise_for_status(self):
        pass

    def json(self):
        return {"results": self._results}


def _results(*scores):
    return [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]


class TestBuildRichName:
    def test_name_only(self):
        assert _build_rich_name("阿斯利康", None) == "阿斯利康"

    def test_with_entity_data(self):
        data = {"name": "阿斯利康", "name_en": "AstraZeneca", "aliases": ["AZ"]}
        assert _build_rich_name("阿斯利康", data) == "阿斯利康 | AstraZeneca | AZ"

    def test_dedup_name(self):
        # data.name 与 entity_name 相同，不应重复
        data = {"name": "阿斯利康"}
        assert _build_rich_name("阿斯利康", data) == "阿斯利康"


class TestBuildRichText:
    def test_basic(self):
        c = CandidateEntity(
            dict_id=1,
            name="A",
            canonical_data={"name": "A", "name_en": "AEN"},
            aliases=["a1"],
        )
        assert _build_rich_text(c) == "A | AEN | a1"


class TestRankWithReranker:
    def test_no_candidates(self):
        r = rank_with_reranker("X", "Drug", None, "", [])
        assert r.status == STATUS_NEW_ENTITY
        assert r.match_step == "step5_reranker"

    def test_high_score_match(self):
        cands = _make_candidates("A", "B")
        with patch(
            "disambiguation.llm_ranker.requests.post",
            return_value=_FakeResponse(_results(0.9, 0.6)),
        ):
            r = rank_with_reranker("X", "Drug", None, "", cands)
        assert r.status == STATUS_CANDIDATE_MATCHED
        assert r.dict_id == 1  # top1 = A (dict_id=1)
        assert r.confidence == 0.9

    def test_high_score_small_gap_ambiguous(self):
        cands = _make_candidates("A", "B")
        with patch(
            "disambiguation.llm_ranker.requests.post",
            return_value=_FakeResponse(_results(0.9, 0.85)),
        ):
            r = rank_with_reranker("X", "Drug", None, "", cands)
        assert r.status == STATUS_AMBIGUOUS

    def test_mid_score_ambiguous(self):
        cands = _make_candidates("A", "B")
        with patch(
            "disambiguation.llm_ranker.requests.post",
            return_value=_FakeResponse(_results(0.6, 0.3)),
        ):
            r = rank_with_reranker("X", "Drug", None, "", cands)
        assert r.status == STATUS_AMBIGUOUS

    def test_low_score_new_entity(self):
        cands = _make_candidates("A", "B")
        with patch(
            "disambiguation.llm_ranker.requests.post",
            return_value=_FakeResponse(_results(0.3, 0.2)),
        ):
            r = rank_with_reranker("X", "Drug", None, "", cands)
        assert r.status == STATUS_NEW_ENTITY

    def test_call_failed(self):
        cands = _make_candidates("A")
        with patch(
            "disambiguation.llm_ranker.requests.post",
            side_effect=Exception("boom"),
        ):
            r = rank_with_reranker("X", "Drug", None, "", cands)
        assert r.status == STATUS_AMBIGUOUS
        assert r.match_step == "step5_reranker_error"

    def test_empty_results(self):
        cands = _make_candidates("A")
        with patch(
            "disambiguation.llm_ranker.requests.post",
            return_value=_FakeResponse([]),
        ):
            r = rank_with_reranker("X", "Drug", None, "", cands)
        assert r.status == STATUS_NEW_ENTITY
