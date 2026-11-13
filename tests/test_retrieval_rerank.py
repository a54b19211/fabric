"""The characters a rerank sends are counted in its receipt, and the count holds nothing back (task book 02, 2.5)."""
from store import retrieval
from store.history_models import HistoryError
from store.index_publication import IndexTarget

TARGET = IndexTarget(source_instance="synthetic", source_id="who_don", is_demo=False, knowledge_base_id="kb-1",
                     source_scope="public_health")


def passages(*sizes):
    return [{"source_id": "who_don", "is_demo": False, "version_id": f"v{n:05d}", "start": 0, "end": size,
             "text": "x" * size, "recall_score": 0.5} for n, size in enumerate(sizes)]


class Provider:
    """A rerank stand-in: scores every document, or fails."""

    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def rerank(self, body):
        self.sent.append(body)
        if self.fail:
            raise HistoryError("index_http_503", "the rerank service did not answer", 503)
        return {"scores": [{"index": n, "relevance_score": 1 - n / 100000} for n in range(len(body["documents"]))],
                "rerank_model": {"model_id": "synthetic"}, "usage": None, "usage_status": "unknown"}


def test_the_receipt_counts_the_calls_the_batch_and_the_characters_after_the_cut():
    provider = Provider()
    receipt = retrieval._rerank(provider, "query", [TARGET], passages(9000, 100))
    assert receipt["state"] == "succeeded"
    assert (receipt["provider_calls"], receipt["documents"], receipt["input_chars"]) == (1, 2, 8100)  # 9000 is cut to 8000
    assert receipt["retries"] == "unknown"  # Fabric sent once; whether the bridge retried is not reported
    [call] = receipt["calls"]
    assert (call["knowledge_base_id"], call["documents"], call["input_chars"]) == ("kb-1", 2, 8100)
    assert call["usage_status"] == "unknown"  # the provider's own usage stays as the bridge reported it
    assert sum(map(len, provider.sent[0]["documents"])) == receipt["input_chars"]
    idle = retrieval._rerank(provider, "query", [TARGET], [])
    assert (idle["state"], idle["provider_calls"], idle["input_chars"], idle["retries"]) == ("not_run", 0, 0, 0)


def test_a_rerank_over_the_planned_twenty_million_characters_still_runs_and_is_counted():
    provider = Provider()
    receipt = retrieval._rerank(provider, "query", [TARGET], passages(*[8000] * 2600))  # 20.8 million characters
    assert receipt["state"] == "succeeded" and receipt["input_chars"] == 2600 * 8000
    assert len(provider.sent) == 1 and "rerank_budget_spent" not in str(receipt)


def test_a_failed_rerank_is_counted_and_falls_back_to_the_recall_order():
    items = passages(500, 700)
    receipt = retrieval._rerank(Provider(fail=True), "query", [TARGET], items)
    assert (receipt["state"], receipt["fallback"]) == ("failed", "reciprocal_rank_fusion")
    assert (receipt["provider_calls"], receipt["documents"], receipt["input_chars"]) == (1, 2, 1200)
    assert [item["version_id"] for item in items] == ["v00000", "v00001"]
