"""新增来源预算与发表循环的故障边界，不访问网络或现役库。"""

import json
import sys

import pytest

from fetch.source_refresh import SourceWatch, watches
from run import scheduler
from store.history_models import HistoryError


def sec_watch(**overrides):
    request = {"source_id": "sec_companyfacts", "query": "*",
               "source_record_ids": ["CIK0000012345:us-gaap:NetIncomeLoss",
                                     "CIK0000012345:us-gaap:Revenues"],
               "max_pages": 2, "max_records": 2}
    return {"id": "sec-fixture", "request": {**request, **overrides}}


@pytest.mark.parametrize("change", [{"source_record_ids": []}, {"max_pages": 1},
                                   {"max_records": 1}, {"query": "broad inference"}])
def test_sec_watch_rejects_scope_that_cannot_finish(change):
    with pytest.raises(ValueError):
        SourceWatch.model_validate(sec_watch(**change))


def test_sec_watch_explicit_scope_survives_config_round_trip(monkeypatch):
    value = sec_watch()
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps([value]))
    watch = watches()[0]
    assert watch.request.source_record_ids == value["request"]["source_record_ids"]
    assert watch.request.query == "*" and watch.request.max_pages == 2


def test_publish_loop_no_longer_runs_indexing():
    # v3 3.6：索引由独立 worker 执行（run.index worker），发表循环不再碰索引
    assert not hasattr(scheduler, "index_tick")


def test_source_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["publisher", "--once"])
    monkeypatch.setattr(scheduler, "publisher_heartbeat", lambda *_: None)

    def unavailable():
        raise HistoryError("synthetic_source_failure", "explicit fault", 503)

    monkeypatch.setattr(scheduler, "refresh_once", unavailable)
    with pytest.raises(SystemExit) as exc:
        scheduler.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("state", ["failed", "blocked"])
def test_reported_source_failure_exits_nonzero(monkeypatch, state):
    monkeypatch.setattr(sys, "argv", ["publisher", "--once"])
    monkeypatch.setattr(scheduler, "publisher_heartbeat", lambda *_: None)
    monkeypatch.setattr(scheduler, "refresh_once", lambda: [{"state": state}])
    monkeypatch.setattr(scheduler, "publish_once", lambda: {"state": "idle"})
    with pytest.raises(SystemExit) as exc:
        scheduler.main()
    assert exc.value.code == 1


def test_index_worker_keeps_going_after_an_outage_and_sleeps_only_when_idle(monkeypatch):
    from store import index_publication as index

    answers = [RuntimeError("synthetic database outage"), {"ready": 2}, {}]
    slept = []

    def work_once(*, client):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    class Stop:
        def is_set(self):
            return False

        def wait(self, seconds):
            slept.append(seconds)

    monkeypatch.setattr(index, "targets", lambda: [])
    monkeypatch.setattr(index, "scan_committed", lambda: None)
    monkeypatch.setattr(index, "work_once", work_once)
    monkeypatch.setattr(index, "WeKnoraIndexClient", lambda: type("Client", (), {"close": lambda self: None})())
    assert index.run_worker(rounds=3, idle_seconds=7, stop=Stop()) == {"unavailable": 1, "ready": 2}
    assert slept == [7, 7]  # after the outage and when nothing was due; not after a busy round
