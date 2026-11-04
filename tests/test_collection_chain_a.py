"""唯一采集框架（来源目录、增量窗口检查点、实时/回填）和登记差分处理器；不联网、不连库。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from biz_rules.history_models import FetchRequest, HistoryError
from collection import catalog, rate
from collection.windows import cycle_request
from db.source_refresh import SourceWatch, _key, watches
from processing import registry_diff as rd

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def ct_watch(**extra):
    return SourceWatch.model_validate(
        {"id": "ct", "request": {"source_id": "clinicaltrials", "query": "*", "max_records": 100}, **extra}
    )


# ——来源目录——


def test_watchable_set_equals_previous_allowlist():
    # 改造前写死的四个来源，现在由目录推出；没有多放也没有少放
    assert catalog.WATCHABLE == {"who_don", "europe_pmc", "clinicaltrials", "sec_companyfacts"}
    assert {"pubmed", "fda_crl"} <= catalog.FETCHABLE


def test_blocked_and_declared_sources_are_visible_gaps_not_fetchable():
    gaps = {g["source_id"]: g for g in catalog.gaps()}
    for source in ("cde_ctr", "chictr", "nmpa", "cde", "cninfo", "hkex", "biorxiv", "drugs_fda"):
        assert source in gaps and source not in catalog.FETCHABLE
    assert "反爬" in gaps["cde_ctr"]["reason"] or "脚本挑战" in gaps["cde_ctr"]["reason"]
    with pytest.raises(ValueError):
        FetchRequest.model_validate({"source_id": "chictr", "query": "x"})


def test_every_spec_declares_decision_rate_group_and_incremental_semantics():
    for spec in catalog.SPECS:
        assert spec.decisions and set(spec.decisions) <= set("ABCDE")
        assert spec.rate_group and spec.min_interval_seconds > 0
        if spec.watchable:
            assert spec.incremental.semantics and spec.incremental.kind != "none"


def test_read_public_refuses_unconnected_plugin(monkeypatch):
    from biz_rules import history_sources

    monkeypatch.setitem(history_sources.PLUGINS, "who_don", None)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    request = FetchRequest.model_validate(
        {"source_id": "who_don", "query": "*", "updated_since": "2026-11-01T00:00:00Z"}
    )
    with pytest.raises(HistoryError) as exc:
        history_sources.read_public(request)
    assert exc.value.code == "source_plugin_missing"


# ——调度：实时/回填、上限、旧键不变——


def test_existing_realtime_watch_keeps_its_state_key():
    from biz_rules.history_models import canonical, sha256

    watch = ct_watch()
    legacy = watch.model_dump(mode="json")
    legacy.pop("mode")
    assert _key(watch) == "source-watch:" + sha256(canonical(legacy))
    assert _key(ct_watch(mode="backfill")) != _key(watch)


def test_backfill_only_for_sources_with_reliable_full_paging():
    ct_watch(mode="backfill")
    with pytest.raises(ValueError):
        SourceWatch.model_validate(
            {"id": "w", "mode": "backfill", "request": {"source_id": "who_don", "query": "*"}}
        )


def test_realtime_watches_run_before_backfill(monkeypatch):
    values = [
        {"id": "full", "mode": "backfill", "request": {"source_id": "clinicaltrials", "query": "*"}},
        {"id": "live", "request": {"source_id": "clinicaltrials", "query": "*"}},
    ]
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps(values))
    assert [w.id for w in watches()] == ["live", "full"]


def test_watch_limit_is_configurable_and_bounded(monkeypatch):
    values = [{"id": f"w{i}", "request": {"source_id": "clinicaltrials", "query": f"q{i}"}} for i in range(9)]
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps(values))
    assert len(watches()) == 9  # 默认上限 32，旧上限 8 已放开
    monkeypatch.setenv("DATAFABRIC_HISTORY_MAX_WATCHES", "8")
    with pytest.raises(HistoryError):
        watches()
    monkeypatch.setenv("DATAFABRIC_HISTORY_MAX_WATCHES", "999")
    with pytest.raises(HistoryError):
        watches()


def test_unconnected_source_cannot_be_watched(monkeypatch):
    monkeypatch.setenv(
        "DATAFABRIC_HISTORY_WATCHES", json.dumps([{"id": "p", "request": {"source_id": "pubmed", "query": "x"}}])
    )
    with pytest.raises(HistoryError):
        watches()


# ——增量窗口与持久检查点——


def test_first_cycle_without_checkpoint_matches_previous_lookback():
    request, start, end = cycle_request(ct_watch(lookback_days=30), {}, NOW)
    assert request["query"] == "AREA[LastUpdatePostDate]RANGE[2026-10-05,2026-11-04]"
    assert start == NOW - timedelta(days=30) and end == NOW


def test_checkpoint_resumes_after_downtime_with_overlap():
    # 停了 40 天：从检查点减重叠续读，而不是只回看 lookback 的 3 天
    state = {"window_checkpoint": (NOW - timedelta(days=40)).isoformat()}
    request, start, _ = cycle_request(ct_watch(lookback_days=3), state, NOW)
    assert start == NOW - timedelta(days=42)
    assert "RANGE[2026-09-23,2026-11-04]" in request["query"]


def test_backfill_and_fixed_ids_have_no_window():
    request, start, end = cycle_request(ct_watch(mode="backfill"), {"window_checkpoint": NOW.isoformat()}, NOW)
    assert request["query"] == "*" and start is None and end is None
    sec = SourceWatch.model_validate(
        {
            "id": "sec",
            "request": {
                "source_id": "sec_companyfacts",
                "query": "*",
                "source_record_ids": ["CIK0000012345:us-gaap:Revenues"],
                "max_pages": 1,
                "max_records": 1,
            },
        }
    )
    assert cycle_request(sec, {}, NOW)[1] is None


def test_europe_pmc_and_who_windows():
    epmc = SourceWatch.model_validate({"id": "e", "request": {"source_id": "europe_pmc", "query": "CRISPR"}})
    request, _, _ = cycle_request(epmc, {"window_checkpoint": "2026-11-03T00:00:00+00:00"}, NOW)
    assert request["query"] == "(CRISPR) AND UPDATE_DATE:[2026-11-01 TO 2026-11-04]"
    who = SourceWatch.model_validate({"id": "w", "request": {"source_id": "who_don", "query": "*"}})
    request, start, end = cycle_request(who, {}, NOW)
    assert request["updated_since"] == start.isoformat() and request["updated_until"] == end.isoformat()


def test_rate_gate_local_fallback_spaces_requests(monkeypatch):
    monkeypatch.delenv("DATAFABRIC_HISTORY_REDIS_URL", raising=False)
    rate._redis.cache_clear()
    slept = []
    clock = [100.0]
    monkeypatch.setattr(rate.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rate.time, "sleep", lambda s: slept.append(round(s, 3)))
    rate._local_next.clear()
    rate.wait("g", 0.5)
    rate.wait("g", 0.5)
    rate.wait("other", 0.5)
    assert slept == [0.5]


# ——登记差分——


BASE = {
    "trial_id": "NCT01234567",
    "status": "RECRUITING",
    "phase": ["PHASE3"],
    "enrollment": 400,
    "enrollment_type": "ESTIMATED",
    "primary_completion_date": "2026-06",
    "primary_completion_date_type": "ESTIMATED",
    "start_date": "2024-01-15",
    "interventions": [{"type": "DRUG", "name": "XYZ-101"}, {"type": "DRUG", "name": "Placebo"}],
    "locations": [{"facility": "A"}, {"facility": "B"}],
    "has_results": False,
}
LOC = {"status": {"json_pointer": "/protocolSection/statusModule/overallStatus"}}


def changed(**new):
    after = {**BASE, **new}
    return {c["field"]: c for c in rd.diff(BASE, after, LOC)}


def test_identical_versions_have_no_changes():
    assert rd.diff(BASE, dict(BASE)) == []


def test_status_change_is_material_and_keeps_pointer_and_both_values():
    c = changed(status="TERMINATED", why_stopped="Sponsor decision")
    assert c["status"]["material"] and c["status"]["json_pointer"].endswith("overallStatus")
    assert c["status"]["old_value"] == {"present": True, "value": "RECRUITING"}
    assert c["why_stopped"]["old_value"] == {"present": False}  # 缺字段不当 null/空串
    assert "TERMINATED" in rd.describe(c["status"])
    # 只复述登记，不给“失败/疗效”结论
    assert "失败" not in rd.describe(c["status"])


def test_date_shift_threshold_and_direction():
    c = changed(primary_completion_date="2027-03")
    assert c["primary_completion_date"]["detail"] == {"days": 273, "precision": "month", "direction": "later"}
    assert c["primary_completion_date"]["material"]
    small = changed(primary_completion_date="2026-07")
    assert not small["primary_completion_date"]["material"]
    start = changed(start_date="2025-01-15")
    assert not start["start_date"]["material"]  # 开始日期的修订不单独触发判断


def test_estimated_to_actual_and_results_posted():
    c = changed(primary_completion_date_type="ACTUAL", enrollment_type="ACTUAL", has_results=True)
    assert c["primary_completion_date_type"]["material_reason"] == "date_now_actual"
    assert c["enrollment_type"]["material"] and c["has_results"]["material"]


def test_enrollment_ratio_threshold():
    assert not changed(enrollment=410)["enrollment"]["material"]
    big = changed(enrollment=250)["enrollment"]
    assert big["material"] and big["detail"]["delta"] == -150


def test_intervention_rename_vs_wording_and_locations_not_material():
    c = changed(interventions=[{"type": "DRUG", "name": "XYZ-101", "description": "new text"},
                               {"type": "DRUG", "name": "Placebo"}])
    assert not c["interventions"]["material"]  # 名称集合没变，只改描述
    arm = changed(interventions=[{"type": "DRUG", "name": "XYZ-101"}])
    assert arm["interventions"]["material"] and arm["interventions"]["detail"]["removed"] == ["Placebo"]
    loc = changed(locations=[{"facility": "A"}])
    assert not loc["locations"]["material"] and loc["locations"]["detail"]["count_after"] == 1


def test_long_values_stored_as_hash_and_size():
    long_outcomes = [{"measure": "m" * 100, "timeFrame": str(i)} for i in range(40)]
    c = changed(results_outcome_measures={"outcomeMeasures": long_outcomes})
    stored = c["results_outcome_measures"]["new_value"]
    assert stored["present"] and "sha256" in stored and "value" not in stored


def test_change_id_is_stable_and_versioned():
    a = rd.change_id("a" * 64, "b" * 64, "status")
    assert a == rd.change_id("a" * 64, "b" * 64, "status") and a != rd.change_id("a" * 64, "c" * 64, "status")
