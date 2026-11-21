"""Withdrawing an index entry (store.index_publication._withdraw): withdrawn = the knowledge deleted in WeKnora."""

from store.index_publication import _withdraw


class Bridge:
    def __init__(self, receipt):
        self.receipt, self.calls = receipt, []

    def disable(self, pid, reason):
        self.calls.append((pid, reason))
        return self.receipt


def row(**changes):
    value = {"remote_id": "p1", "first_send_at": "t", "knowledge_id": "k1", "receipt": None, "error": None}
    value.update(changes)
    return value


GONE = {"source_status": "disabled", "vector_count": 0, "index_status": "disabled", "vector_status": "disabled"}


def test_a_duplicate_that_never_became_knowledge_is_withdrawn_locally():
    bridge = Bridge(GONE)
    blocked = row(knowledge_id=None, error={"code": "index_duplicate_content", "message": "m"})
    assert _withdraw(blocked, bridge, "revoked")["state"] == "disabled" and bridge.calls == []


def test_a_sent_entry_is_disabled_only_once_the_bridge_reports_nothing_left():
    assert _withdraw(row(), Bridge(GONE), "superseded")["state"] == "disabled"
    pending = dict(GONE, index_status="pending", vector_status="pending", vector_count=2)
    assert _withdraw(row(), Bridge(pending), "superseded")["state"] == "disabling"
