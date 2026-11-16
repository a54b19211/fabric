"""The clean stage's order of work: sources take turns, newest versions first (no database)."""

from clean import clean_stage


def test_sources_take_turns_and_a_finished_source_is_not_asked_again(monkeypatch):
    left = {"clinicaltrials": 3, "nmpa": 0, "who_don": 1}  # batches of work per source
    calls = []

    def one_batch(engine, key, batch, counts, source):
        calls.append(source)
        took = min(left[source], 1)
        left[source] -= took
        return took

    monkeypatch.setattr("store.records.fabric_engine", lambda: None)
    monkeypatch.setattr(clean_stage, "_process_waiting", lambda *args, **kwargs: 0)
    monkeypatch.setattr(clean_stage, "_sources", lambda engine: list(left))
    monkeypatch.setattr(clean_stage, "_process_batch", one_batch)
    clean_stage.process()
    # a large backlog (clinicaltrials) does not hold back the other sources; who_don is reached in the first round
    assert calls == ["clinicaltrials", "nmpa", "who_don", "clinicaltrials", "who_don", "clinicaltrials",
                     "clinicaltrials"]


def test_a_source_batch_takes_its_newest_versions_first():
    assert "v.source_id=:source" in clean_stage._TODO_SQL
    assert "ORDER BY v.published_seq DESC" in clean_stage._TODO_SQL
