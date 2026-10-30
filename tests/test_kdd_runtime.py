"""新增来源预算与发表循环的故障边界，不访问网络或现役库。"""

import json
import sys

import pytest

from biz_rules.history_models import HistoryError
from db.source_refresh import SourceWatch, watches
from flows import history_publish


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


def test_index_audit_runs_when_primary_source_refresh_fails(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["publisher", "--once", "--backfill-batch-size", "0"])
    monkeypatch.setattr(history_publish, "publisher_heartbeat", lambda *_: None)
    calls = []

    def unavailable():
        raise HistoryError("synthetic_source_failure", "explicit fault", 503)

    monkeypatch.setattr(history_publish, "refresh_once", unavailable)
    monkeypatch.setattr(history_publish, "index_tick",
                        lambda **kw: calls.append(kw) or {"state": "running", "processed": ["idle"]})
    with pytest.raises(SystemExit) as exc:
        history_publish.main()
    assert exc.value.code == 1
    assert calls == [{"batch_size": 5}]


def test_index_failure_does_not_claim_successful_once_execution(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["publisher", "--once", "--backfill-batch-size", "0"])
    monkeypatch.setattr(history_publish, "publisher_heartbeat", lambda *_: None)
    monkeypatch.setattr(history_publish, "refresh_once", lambda: [])
    monkeypatch.setattr(history_publish, "publish_once", lambda: {"state": "idle"})
    monkeypatch.setattr(history_publish, "index_tick",
                        lambda **kw: {"state": "running", "processed": ["waiting", "blocked"]})
    with pytest.raises(SystemExit) as exc:
        history_publish.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("state", ["failed", "blocked"])
def test_reported_source_failure_still_audits_index_and_exits_nonzero(monkeypatch, state):
    monkeypatch.setattr(sys, "argv", ["publisher", "--once", "--backfill-batch-size", "0"])
    monkeypatch.setattr(history_publish, "publisher_heartbeat", lambda *_: None)
    monkeypatch.setattr(history_publish, "refresh_once", lambda: [{"state": state}])
    monkeypatch.setattr(history_publish, "publish_once", lambda: {"state": "idle"})
    calls = []
    monkeypatch.setattr(history_publish, "index_tick",
                        lambda **kw: calls.append(True) or {"state": "running", "processed": ["idle"]})
    with pytest.raises(SystemExit) as exc:
        history_publish.main()
    assert exc.value.code == 1 and calls == [True]
