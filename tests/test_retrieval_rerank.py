"""Rerank: verified passages go to the bridge once per knowledge base and scope, cut to RERANK_PASSAGE_CHARS, and are
sorted by score; a failed rerank falls back to the recall order."""
from store import retrieval
from store.index_publication import IndexTarget
from store.record_models import FabricError

TARGET = IndexTarget(source_instance="synthetic", source_id="who_don", is_demo=False, knowledge_base_id="kb-1",
                     source_scope="public_health")


def passages(*sizes):
    return [{"source_id": "who_don", "is_demo": False, "version_id": f"v{n:05d}", "start": 0, "end": size,
             "text": "x" * size, "recall_score": 0.5} for n, size in enumerate(sizes)]


class Provider:
    """A rerank stand-in: scores the documents in reverse order, or fails."""

    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def rerank(self, body):
        self.sent.append(body)
        if self.fail:
            raise FabricError("index_http_503", "the rerank service did not answer", 503)
        return {"scores": [{"index": n, "relevance_score": n / 10} for n in range(len(body["documents"]))],
                "rerank_model": {"model_id": "synthetic"}, "usage": None}


def test_passages_are_cut_sent_once_per_knowledge_base_and_sorted_by_score():
    provider, items = Provider(), passages(9000, 100)
    receipt = retrieval._rerank(provider, "query", [TARGET], items)
    assert receipt["state"] == "succeeded" and len(provider.sent) == 1
    [call] = receipt["calls"]
    assert (call["knowledge_base_id"], call["documents"], call["input_chars"]) == ("kb-1", 2, 8100)  # 9000 cut to 8000
    assert [item["version_id"] for item in items] == ["v00001", "v00000"]  # by the rerank score
    assert retrieval._rerank(provider, "query", [TARGET], []) == {"state": "not_run", "calls": []}


def test_a_failed_rerank_falls_back_to_the_recall_order():
    items = passages(500, 700)
    receipt = retrieval._rerank(Provider(fail=True), "query", [TARGET], items)
    assert (receipt["state"], receipt["fallback"]) == ("failed", "reciprocal_rank_fusion")
    assert [item["version_id"] for item in items] == ["v00000", "v00001"]
