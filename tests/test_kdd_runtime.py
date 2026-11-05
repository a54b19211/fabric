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


def test_publish_loop_no_longer_runs_indexing():
    # v3 3.6：索引由独立 worker 执行（migration.index_publication worker），发表循环不再碰索引
    assert not hasattr(history_publish, "index_tick")


def test_source_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["publisher", "--once", "--backfill-batch-size", "0"])
    monkeypatch.setattr(history_publish, "publisher_heartbeat", lambda *_: None)

    def unavailable():
        raise HistoryError("synthetic_source_failure", "explicit fault", 503)

    monkeypatch.setattr(history_publish, "refresh_once", unavailable)
    with pytest.raises(SystemExit) as exc:
        history_publish.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("state", ["failed", "blocked"])
def test_reported_source_failure_exits_nonzero(monkeypatch, state):
    monkeypatch.setattr(sys, "argv", ["publisher", "--once", "--backfill-batch-size", "0"])
    monkeypatch.setattr(history_publish, "publisher_heartbeat", lambda *_: None)
    monkeypatch.setattr(history_publish, "refresh_once", lambda: [{"state": state}])
    monkeypatch.setattr(history_publish, "publish_once", lambda: {"state": "idle"})
    with pytest.raises(SystemExit) as exc:
        history_publish.main()
    assert exc.value.code == 1


def test_index_worker_rotates_lanes_and_survives_unavailable_database(monkeypatch):
    from db import index_publication

    claims, scans = [], []

    def process_once(*, lane, sent):
        claims.append((lane, sent))
        if len(claims) == 3:
            raise RuntimeError("synthetic database outage")
        return {"state": "ready"}

    def scan():
        scans.append(True)
        raise RuntimeError("synthetic scan outage")

    monkeypatch.setattr(index_publication, "process_once", process_once)
    monkeypatch.setattr(index_publication, "scan_committed", scan)
    counts = index_publication.run_worker(concurrency=1, rounds=8, idle_seconds=0, scan_every_seconds=0)
    assert len(scans) == 8 and sum(counts[0].values()) == 8 and counts[0]["unavailable"] == 8
    # 扫描失败的轮次不处理作业；扫描恢复后按 3:1 轮换新增与回填（见 SLOTS）
    monkeypatch.setattr(index_publication, "scan_committed", lambda: None)
    claims.clear()
    counts = index_publication.run_worker(concurrency=1, rounds=8, idle_seconds=0, scan_every_seconds=1e9)
    assert counts[0] == {"ready": 7, "unavailable": 1}
    assert [lane for lane, _ in claims].count("backfill") == 2 and len(claims) == 8
