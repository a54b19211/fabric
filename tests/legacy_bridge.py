"""The old legacy_documents writer, for the contract tests that still drive it (kept per RULES-r2: compatibility
implementations are not deleted). The service never opens it: there is no environment switch. A test that needs the old
writer, or a graph package that cites a legacy version as packages did before the retirement, says so here."""

from graph.publish import history_graph
from store import history

REFUSE_RETIRED_REFS = history_graph._refuse_retired_refs  # what the service runs, taken before any test replaces it


def open_old_writer(monkeypatch):
    monkeypatch.setattr(history, "legacy_retired", lambda: False)
    monkeypatch.setattr(history_graph, "_refuse_retired_refs", lambda refs: None)


def close_old_writer(monkeypatch):
    """Back to what the service does."""
    monkeypatch.setattr(history, "legacy_retired", lambda: True)
    monkeypatch.setattr(history_graph, "_refuse_retired_refs", REFUSE_RETIRED_REFS)
