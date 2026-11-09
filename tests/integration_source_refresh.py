"""真实隔离PG：逐站回填、不可变观测绑定、固定watch续页及源健康；不碰现役。"""

import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import integration_publication as publication_fixtures
import pytest
from integration_publication import insert_document, rows
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_publication import snapshot
from test_source_refresh import metric, who_value

from biz_rules.history_models import (
    ArchiveImport,
    ArchiveRecord,
    HistoryError,
    canonical,
    sha256,
)
from db import history, publication, source_refresh
from db.models import CrawlLog

databases = publication_fixtures.databases


def allow(monkeypatch, sites):
    monkeypatch.setenv("DATAFABRIC_PUBLICATION_SOURCES", json.dumps(sites))
    monkeypatch.setenv(
        "DATAFABRIC_HISTORY_SOURCE_ALLOWLIST",
        json.dumps(
            [
                "external_archive",
                "legacy_documents",
                "reviewed_graph",
                "clinicaltrials",
                "nmpa",
                "cde",
                "who_don",
                "europe_pmc",
            ]
        ),
    )


def clean_inventory(source, sites, count=3):
    for site in sites:
        for index in range(count):
            value = snapshot(site + "-" + str(index))
            value["source_site"] = site
            insert_document(source, value)
    with source.begin() as conn:
        conn.execute(text("DELETE FROM publication_outbox"))


def test_per_site_round_robin_total_budget_and_restart_progress(databases, monkeypatch):
    source, _ = databases
    allow(monkeypatch, ["clinicaltrials", "nmpa", "cde"])
    clean_inventory(source, ["clinicaltrials", "nmpa", "cde"], 3)
    first = publication.backfill_once(limit=2)
    assert first["scanned"] == 2 and first["seeded"] == 2
    progressed = {site for site, state in first["sites"].items() if state["last_id"]}
    assert progressed == {"cde", "clinicaltrials"}
    assert first["sites"]["nmpa"]["last_id"] == 0 and not first["complete"]
    with source.begin() as conn:
        conn.execute(
            text(
                "UPDATE publication_outbox SET state='published' WHERE kind='document'"
            )
        )
    second = publication.backfill_once(limit=2)
    assert second["sites"]["nmpa"]["last_id"] > 0 and second["scanned"] == 2
    assert second["max_id"] == first["max_id"]
    # Later inserts are captured by triggers, never silently included in old inventory scope.
    insert_document(source, {**snapshot("after-inventory"), "source_site": "nmpa"})
    saved = rows(
        source, "SELECT snapshot FROM publication_outbox WHERE kind='backfill'"
    )[0]["snapshot"]
    assert saved["max_id"] == first["max_id"]


def test_old_global_scope_migrates_only_same_sites_and_fixed_upper(
    databases, monkeypatch
):
    source, _ = databases
    sites = ["clinicaltrials", "nmpa"]
    allow(monkeypatch, sites)
    clean_inventory(source, sites, 2)
    with source.begin() as conn:
        upper = conn.execute(text("SELECT max(id) FROM documents")).scalar_one()
        key = "backfill:" + sha256(canonical(sites))
        conn.execute(
            text("""
          INSERT INTO publication_outbox(event_key,kind,source_doc_uuid,snapshot)
          VALUES (:key,'backfill',:key,CAST(:scope AS jsonb))
        """),
            {
                "key": key,
                "scope": canonical(
                    {"source_sites": sites, "last_id": 1, "max_id": upper}
                ),
            },
        )
    result = publication.backfill_once(limit=1)
    assert result["max_id"] == upper
    assert result["sites"]["nmpa"]["last_id"] == 1
    assert result["sites"]["clinicaltrials"]["last_id"] >= 1


def test_site_query_failure_rotates_and_preserves_failed_cursor(databases, monkeypatch):
    source, _ = databases
    allow(monkeypatch, ["clinicaltrials", "nmpa"])
    clean_inventory(source, ["clinicaltrials", "nmpa"], 1)

    def fail_one(conn, cursor, statement, parameters, context, executemany):
        if (
            "FROM documents d" in statement
            and parameters.get("site") == "clinicaltrials"
        ):
            raise SQLAlchemyError("SYNTHETIC source query failure")

    event.listen(source, "before_cursor_execute", fail_one)
    try:
        result = publication.backfill_once(limit=1)
    finally:
        event.remove(source, "before_cursor_execute", fail_one)
    assert result["sites"]["clinicaltrials"]["last_id"] == 0
    assert result["sites"]["clinicaltrials"]["last_error"]["code"] == "seed_read_failed"
    assert result["sites"]["nmpa"]["complete"] and result["seeded"] == 1


def archive_one(native, data, *, proof=None):
    item = ArchiveRecord(
        source_record_id=native, record_kind="document", raw_content=json.dumps(data)
    )
    return ArchiveImport(
        source_id="external_archive",
        is_demo=True,
        scope_description="SYNTHETIC scoped observation",
        declared_count=1,
        records=[item],
        proof=proof,
    )


def current_record(run, native):
    return history.read_version(
        run.version_ids[0],
        "external_archive",
        native,
        True,
        "current",
        datetime.now(timezone.utc),
    )


def bound_metric(native):
    data = metric()
    data["basis"] = {
        "kind": "source_bound",
        "ref": {
            key: getattr(native, key)
            for key in ("source_id", "source_record_id", "version_id", "raw_sha256")
        },
        "field_pointers": {
            "subject.id": "/company",
            "metric_id": "/metric",
            "metric_version": "/definition",
            "value.lower": "/amount",
            "value.upper": "/amount",
            "unit": "/currency",
            "population": "/population",
            "region": "/region",
            "period_start": "/start",
            "period_end": "/end",
            "measurement_basis": "/basis",
            "status": "/status",
        },
    }
    return data


def native_finance():
    return {
        "record_kind": "document",
        "title": "SYNTHETIC native finance",
        "company": "COMPANY1",
        "metric": "revenue",
        "definition": "quarterly-v1",
        "amount": 100,
        "currency": "USD",
        "population": "consolidated",
        "region": "Global",
        "start": "2020-01-01",
        "end": "2020-04-01",
        "basis": "reported",
        "status": "active",
    }


def test_source_bound_validates_every_native_field_and_rejects_self_cycle(databases):
    _, target = databases
    native = current_record(
        history.import_archive(archive_one("native", native_finance()), "native"),
        "native",
    )
    data = bound_metric(native)
    run = history.import_archive(archive_one("metric", data), "metric")
    saved = current_record(run, "metric")
    assert saved.parser_version == "datafabric.observation.projection.v0.1"
    assert saved.facts["basis"]["kind"] == "source_bound"
    for name, value in (
        ("unit", "EUR"),
        ("population", "different"),
        ("region", "local"),
    ):
        bad = copy.deepcopy(data)
        bad[name] = value
        with pytest.raises(HistoryError, match="不一致"):
            history.import_archive(archive_one("bad-" + name, bad), "bad-" + name)
    bad = copy.deepcopy(data)
    bad["basis"]["ref"]["raw_sha256"] = "0" * 64
    with pytest.raises(HistoryError, match="版本不一致"):
        history.import_archive(archive_one("bad-hash", bad), "bad-hash")
    cycle = bound_metric(saved)
    with pytest.raises(HistoryError, match="不一致"):
        history.import_archive(archive_one("cycle", cycle), "cycle")
    with target.begin() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM history_versions")).scalar_one()
            == 2
        )


def test_supersedes_same_scope_allows_period_correction_not_unrelated_metric(databases):
    first = current_record(
        history.import_archive(archive_one("metric", metric()), "first"), "metric"
    )
    correction = metric()
    correction["period_end"] = "2020-04-02"
    correction["supersedes"] = {
        key: getattr(first, key)
        for key in ("source_id", "source_record_id", "version_id", "raw_sha256")
    }
    second = history.import_archive(archive_one("metric", correction), "correction")
    assert second.version_ids != [first.version_id]
    bad = {**correction, "region": "another"}
    with pytest.raises(HistoryError, match="不一致"):
        history.import_archive(archive_one("metric", bad), "unrelated")
    assert (
        current_record(second, "metric").facts["supersedes"]["version_id"]
        == first.version_id
    )


def test_old_proof_cannot_make_later_bound_evidence_visible(databases):
    from biz_rules.history_models import archive_manifest_hash

    def reviewed(request, day):
        return request.model_copy(
            update={
                "proof": __import__(
                    "biz_rules.history_models", fromlist=["ArchiveProofRequest"]
                ).ArchiveProofRequest(
                    available_at={"value": day, "precision": "day"},
                    uri="fixture://SYNTHETIC-proof",
                    sha256=archive_manifest_hash(request.records),
                    reviewed=True,
                    review_note="SYNTHETIC reviewed test material",
                )
            }
        )

    native_request = reviewed(archive_one("native", native_finance()), "2021-01-01")
    original = current_record(
        history.import_archive(native_request, "later-native"), "native"
    )
    earlier = reviewed(
        archive_one("early-metric", bound_metric(original)), "2020-06-01"
    )
    run = history.import_archive(earlier, "earlier-metric")
    with pytest.raises(HistoryError, match="不可见"):
        history.read_version(
            run.version_ids[0],
            "external_archive",
            "early-metric",
            True,
            "public_history_reconstruction",
            datetime(2020, 12, 31, tzinfo=timezone.utc),
        )


def watch_config(monkeypatch, source="who_don"):
    monkeypatch.setenv(
        "DATAFABRIC_HISTORY_WATCHES",
        json.dumps(
            [
                {
                    "id": "synthetic-watch",
                    "request": {"source_id": source, "query": "*", "max_records": 1},
                    "cadence_seconds": 3600,
                    "lookback_days": 30,
                }
            ]
        ),
    )


def finish_active(source, records, continuation=None, exhausted=False, error=None, **reported):
    control = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    run = history.read_run(control["active_run_id"])
    history.job_input(run.id, run.attempt)
    return history.finish_fetch(
        run.id,
        run.attempt,
        {
            "records": records,
            "complete": False,
            "upstream_pagination_exhausted": exhausted,
            "stop_reason": "upstream_exhausted" if exhausted else "record_budget",
            "error": error,
            "requests": 1,
            "bytes": 100,
            "source_total": None,
            "next_continuation": continuation,
            **reported,
        },
    )


def test_watch_fixed_intent_recovers_unknown_create_and_resumes_next_page(
    databases, monkeypatch
):
    source, target = databases
    allow(monkeypatch, ["clinicaltrials"])
    watch_config(monkeypatch)
    import flows.history_fetch as fetch

    enqueue = Mock()
    monkeypatch.setattr(fetch, "enqueue_fetch", enqueue)

    def active(*args):
        raise HistoryError("job_may_be_active", "SYNTHETIC active", 409)

    monkeypatch.setattr(fetch, "retry_inactive", active)
    original = history.create_fetch
    once = True

    def create_then_lose(*args):
        nonlocal once
        result = original(*args)
        if once:
            once = False
            raise RuntimeError("SYNTHETIC unknown commit")
        return result

    monkeypatch.setattr(history, "create_fetch", create_then_lose)
    with pytest.raises(RuntimeError):
        source_refresh.refresh_once()
    source_refresh.refresh_once()
    assert rows(target, "SELECT count(*) n FROM history_runs")[0]["n"] == 1
    state = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    request = __import__(
        "biz_rules.history_models", fromlist=["FetchRequest"]
    ).FetchRequest.model_validate(state["request"])
    continuation = {
        "scope_hash": request.scope_hash(),
        "cursor": '{"modified":"2026-10-11T12:00:01Z","id":"00000000-0000-4000-8000-000000000001"}',
        "read_count": 1,
    }
    value = who_value()
    finish_active(
        source,
        [
            ArchiveRecord(
                source_record_id=value["Id"],
                record_kind="document",
                raw_content=json.dumps(value),
            )
        ],
        continuation,
    )
    source_refresh.refresh_once()
    paged = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    assert paged["request"]["continuation"] == continuation
    assert "last_success_at" not in paged
    original_until = paged["request"]["updated_until"]
    source_refresh.refresh_once()
    next_state = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    assert next_state["request"]["updated_until"] == original_until
    assert rows(target, "SELECT count(*) n FROM history_runs")[0]["n"] == 2
    finish_active(source, [], exhausted=True)
    source_refresh.refresh_once()
    complete = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    assert (
        complete["last_success_at"]
        and complete["state"] == "idle"
        and "request" not in complete
    )
    source_refresh.refresh_once()
    assert rows(target, "SELECT count(*) n FROM history_runs")[0]["n"] == 2


def test_source_health_unknown_and_actual_failures_are_separate_from_publisher(
    databases, monkeypatch
):
    source, target = databases
    allow(monkeypatch, ["clinicaltrials"])
    health = source_refresh.source_health("clinicaltrials")
    assert health["collection"]["last_check_at"] is None
    assert all(value is None for value in health["collection"]["counts"].values())
    now = datetime.now(timezone.utc)
    with Session(source) as session, session.begin():
        session.add(
            CrawlLog(
                site_key="clinicaltrials",
                status="success",
                started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=1),
                failed_count=0,
                total_count=4,
            )
        )
        session.add(
            CrawlLog(
                site_key="clinicaltrials",
                status="failed",
                started_at=now,
                finished_at=now,
                failed_count=1,
                error_message="SYNTHETIC SECRET",
                crawl_config_snapshot={"token": "SYNTHETIC SECRET"},
            )
        )
    publication.publisher_heartbeat("idle")
    health = source_refresh.source_health("clinicaltrials")
    assert (
        health["collection"]["last_recorded_success_at"]
        and health["collection"]["last_recorded_failure_at"]
    )
    assert (
        health["publication"]["state"] == "idle"
        and health["collection"]["state"] == "failed"
    )
    assert "SYNTHETIC SECRET" not in json.dumps(health)
    with target.begin() as conn:
        conn.execute(
            text(
                "UPDATE history_sources SET read_enabled=false WHERE source_id='clinicaltrials'"
            )
        )
    with pytest.raises(HistoryError) as exc:
        source_refresh.source_health("clinicaltrials")
    assert exc.value.status == 403


def test_source_bound_precision_and_nonstandard_pointer_cannot_get_verified(databases):
    raw = native_finance()
    raw["amount"] = "1.0000000000000000001"
    # JSON native number, not a string: parse_float must preserve these exact digits.
    original_text = json.dumps(raw).replace(
        '"1.0000000000000000001"', "1.0000000000000000001"
    )
    req = ArchiveImport(
        source_id="external_archive",
        is_demo=True,
        scope_description="SYNTHETIC native precision",
        declared_count=1,
        records=[
            ArchiveRecord(
                source_record_id="native",
                record_kind="document",
                raw_content=original_text,
            )
        ],
    )
    native = current_record(history.import_archive(req, "native-precision"), "native")
    data = bound_metric(native)
    data["value"] = {"lower": "1", "upper": "1"}
    with pytest.raises(HistoryError, match="不一致"):
        history.import_archive(archive_one("rounded", data), "rounded")
    data["value"] = {"lower": "1.0000000000000000001", "upper": "1.0000000000000000001"}
    data["basis"]["field_pointers"]["value.lower"] = "/amount/"
    with pytest.raises(HistoryError, match="不一致"):
        history.import_archive(archive_one("bad-pointer", data), "bad-pointer")
    data["basis"]["field_pointers"]["value.lower"] = "/amount"
    assert history.import_archive(archive_one("exact", data), "exact").version_ids


def test_watch_failure_retains_continuation_and_does_not_claim_success(
    databases, monkeypatch
):
    source, _ = databases
    allow(monkeypatch, ["clinicaltrials"])
    watch_config(monkeypatch)
    import flows.history_fetch as fetch

    monkeypatch.setattr(fetch, "enqueue_fetch", lambda run: run)

    def active(*args):
        raise HistoryError("job_may_be_active", "SYNTHETIC active", 409)

    monkeypatch.setattr(fetch, "retry_inactive", active)
    source_refresh.refresh_once()
    state = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    first_request = state["request"]
    finish_active(
        source, [], error={"code": "source_read_failed", "message": "SYNTHETIC failure"}
    )
    source_refresh.refresh_once()
    failed = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    assert failed["request"] == first_request
    assert failed["state"] == "failed" and "last_success_at" not in failed
    assert "active_run_id" not in failed and failed["last_failure_at"]
    assert source_refresh.refresh_once() == []


def test_old_watch_lease_cannot_overwrite_new_owner(databases, monkeypatch):
    source, _ = databases
    allow(monkeypatch, ["clinicaltrials"])
    watch_config(monkeypatch)
    import flows.history_fetch as fetch

    monkeypatch.setattr(fetch, "enqueue_fetch", lambda run: run)

    def active(*args):
        raise HistoryError("job_may_be_active", "SYNTHETIC active", 409)

    monkeypatch.setattr(fetch, "retry_inactive", active)
    source_refresh.refresh_once()
    row = rows(
        source,
        "SELECT event_key,snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]
    with source.begin() as conn:
        conn.execute(
            text(
                "UPDATE publication_outbox SET snapshot=jsonb_set(snapshot,'{lease_token}','\"new-owner\"') WHERE event_key=:key"
            ),
            {"key": row["event_key"]},
        )
    with pytest.raises(HistoryError) as exc:
        source_refresh._save(source, row["event_key"], row["snapshot"])
    assert exc.value.code == "source_watch_lease_lost"
    assert (
        rows(
            source,
            "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
        )[0]["snapshot"]["lease_token"]
        == "new-owner"
    )


def test_independent_keyset_index_creation_and_rollback_in_real_schema(databases):
    from migration import runner

    source, _ = databases
    assert runner.main(["--db", "publication", "--online", "site-index"]) == 0
    definition = rows(
        source,
        "SELECT indexdef FROM pg_indexes WHERE indexname='idx_documents_publication_site_id'",
    )[0]["indexdef"]
    assert "(source_site, id)" in definition
    assert runner.main(["--db", "publication", "--online", "site-index", "--drop"]) == 0
    with source.begin() as conn:
        assert (
            conn.execute(
                text("SELECT to_regclass('idx_documents_publication_site_id')")
            ).scalar_one()
            is None
        )


def test_epmc_watch_persists_update_window_not_publication_date(databases, monkeypatch):
    source, _ = databases
    allow(monkeypatch, ["clinicaltrials"])
    watch_config(monkeypatch, source="europe_pmc")
    import flows.history_fetch as fetch

    monkeypatch.setattr(fetch, "enqueue_fetch", lambda run: run)

    def active(*args):
        raise HistoryError("job_may_be_active", "SYNTHETIC active", 409)

    monkeypatch.setattr(fetch, "retry_inactive", active)
    source_refresh.refresh_once()
    first = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    query = first["request"]["query"]
    assert "UPDATE_DATE:[" in query and "FIRST_PDATE" not in query
    from biz_rules.history_models import FetchRequest

    request = FetchRequest.model_validate(first["request"])
    continuation = {
        "scope_hash": request.scope_hash(),
        "cursor": "next-frozen-page",
        "read_count": 1,
    }
    finish_active(source, [], continuation=continuation)
    source_refresh.refresh_once()
    source_refresh.refresh_once()
    later = rows(
        source,
        "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'",
    )[0]["snapshot"]
    assert later["request"]["query"] == query
    assert later["request"]["continuation"] == continuation


@pytest.mark.parametrize("query,ids", [("*", []), ("lung cancer", []),
                                    ("*", ["NCT00000001"])])
def test_clinical_watch_freezes_update_window_or_exact_ids(databases, monkeypatch, query, ids):
    source, target = databases
    allow(monkeypatch, ["clinicaltrials"])
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps([{
        "id": "ct-fixed-fixture", "lookback_days": 30,
        "request": {"source_id": "clinicaltrials", "query": query, "source_record_ids": ids},
    }]))
    import flows.history_fetch as fetch
    monkeypatch.setattr(fetch, "enqueue_fetch", lambda run: run)

    def active(*_):
        raise HistoryError("job_may_be_active", "SYNTHETIC active", 409)

    monkeypatch.setattr(fetch, "retry_inactive", active)
    source_refresh.refresh_once()
    state = rows(source, "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'")[0]["snapshot"]
    request = state["request"]
    if ids:
        assert request["query"] == query and request["source_record_ids"] == ids
    else:
        expected = "AREA[LastUpdatePostDate]RANGE[" + state["window_started_at"][:10] + "," + state["window_ended_at"][:10] + "]"
        assert request["query"] == (expected if query == "*" else "(" + query + ") AND " + expected)
    source_refresh.refresh_once()
    saved = rows(source, "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'")[0]["snapshot"]
    assert saved["request"] == request and saved["active_run_id"] == state["active_run_id"]
    assert rows(target, "SELECT count(*) n FROM history_runs")[0]["n"] == 1


def test_gdelt_watch_cycle_stopped_by_a_ceiling_keeps_its_gaps_and_resumes_at_the_unread_window(
    databases, monkeypatch
):
    source, _ = databases
    allow(monkeypatch, ["clinicaltrials"])
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", json.dumps(["gdelt_doc"]))
    watch_config(monkeypatch, source="gdelt_doc")
    import flows.history_fetch as fetch

    monkeypatch.setattr(fetch, "enqueue_fetch", lambda run: run)

    def active(*args):
        raise HistoryError("job_may_be_active", "SYNTHETIC active", 409)

    monkeypatch.setattr(fetch, "retry_inactive", active)
    source_refresh.refresh_once()
    watch = "SELECT snapshot FROM publication_outbox WHERE event_key LIKE 'source-watch:%'"
    first = rows(source, watch)[0]["snapshot"]
    since, until = first["request"]["updated_since"], first["request"]["updated_until"]
    assert since.endswith(":00:00+00:00") and until == first["window_ended_at"]  # whole UTC hours
    middle = (datetime.fromisoformat(since) + timedelta(hours=1)).isoformat()
    cut = {"start": since, "end": middle}
    finish_active(
        source, [], stop_reason="outbound_quota_hour", truncated_intervals=[cut],
        window_coverage=[
            {**cut, "status": "read", "possibly_truncated": True, "reason": "finest_window_full"},
            {"start": middle, "end": until, "status": "not_read", "reason": "outbound_quota_hour"},
        ],
    )
    source_refresh.refresh_once()
    state = rows(source, watch)[0]["snapshot"]
    assert state["state"] == "idle" and state["error"] is None and "request" not in state
    assert state["window_checkpoint"] == middle and "last_success_at" not in state
    assert state["last_coverage"]["truncated_intervals"] == [cut]  # the gap stays readable
    assert state["last_coverage"]["stop_reason"] == "outbound_quota_hour"
    assert source_refresh.refresh_once() == []  # next cycle waits for its cadence
