"""The WHO backfill over the real history store (isolated PostgreSQL): its legs are history runs, the receipt lands
in their coverage, and only the versions the backfill created are listed there as background."""

import json

import integration_publication as publication_fixtures
import pytest
from sqlalchemy import text
from test_who_backfill import UNTIL, WhoStandIn

from biz_rules.history_models import FetchRequest
from biz_rules.history_sources import read_public
from collection import rate
from db import history
from flows.who_backfill import SINCE, Counts, backfill_who

databases = publication_fixtures.databases


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    monkeypatch.setattr(rate, "wait_for_url", lambda url, **kw: None)


def test_backfill_runs_carry_the_background_mark_for_the_versions_they_created(databases, monkeypatch):
    _, target = databases
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", json.dumps(["who_don"]))
    who = WhoStandIn(monkeypatch, count=200)
    # a scheduled read adopted the first notice since 2024 before the backfill: it was first seen as new activity
    seed = FetchRequest(source_id="who_don", query="*", max_records=1, max_pages=1, updated_since=SINCE)
    run, _ = history.create_fetch(seed, "seed-scheduled-read")
    history.job_input(run.id, run.attempt)
    ordinary = history.finish_fetch(run.id, run.attempt, read_public(seed))
    assert "backfill" not in ordinary.coverage  # an ordinary run has no mark

    bounds = Counts(records=120, bytes=40 << 20, requests=20)
    receipt = backfill_who(UNTIL, bounds=bounds)
    assert receipt["stopped_by"] == "records_bound" and receipt["spent"]["records"] == 120
    runs = [history.read_run(r) for r in _backfill_run_ids(target)]
    assert [r.coverage["backfill"]["leg"] for r in runs] == [1, 2] and all(r.state == "partial" for r in runs)
    background = [v for r in runs for v in r.coverage["backfill"]["background_version_ids"]]
    adopted = [v for r in runs for v in r.version_ids]
    assert len(adopted) == 120 and len(background) == 119  # 120 read, one of them already adopted by the scheduled read
    assert set(adopted) - set(background) == set(ordinary.version_ids)
    assert runs[0].coverage["new_version_count"] == 99 and runs[0].coverage["same_version_count"] == 1
    requests = len(who.requests)
    assert backfill_who(UNTIL, bounds=bounds) == receipt and len(who.requests) == requests  # nothing sent again
    with target.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one() == 120


def _backfill_run_ids(target):
    with target.begin() as conn:
        return [str(row[0]) for row in conn.execute(
            text("SELECT id FROM history_runs WHERE idempotency_key LIKE 'who-backfill:%' ORDER BY idempotency_key")
        )]
