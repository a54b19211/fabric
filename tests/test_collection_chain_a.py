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
    for source in ("cde_ctr", "cninfo", "hkex", "biorxiv", "drugs_fda"):
        assert source in gaps and source not in catalog.FETCHABLE
    assert "脚本挑战" in gaps["cde_ctr"]["reason"]
    # 旧 ETL 的三个站已迁入为插件：可按需读取，但不进固定刷新、标为未经本系统实测
    for source in ("chictr", "nmpa", "cde"):
        assert source in catalog.FETCHABLE and source not in gaps and not catalog.spec(source).verified
    with pytest.raises(ValueError):
        FetchRequest.model_validate({"source_id": "cde_ctr", "query": "x"})


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


def _gate_worker(args):
    import os
    import time as _time

    directory, n = args
    os.environ["DATAFABRIC_RATE_DIR"] = directory
    os.environ.pop("DATAFABRIC_HISTORY_REDIS_URL", None)
    from collection import rate as gate

    gate._redis.cache_clear()
    stamps = []
    for _ in range(n):
        gate.wait("probe.example", 0.2)
        stamps.append(_time.time())
    return stamps


def test_rate_gate_without_redis_is_shared_across_processes(tmp_path):
    """Codex r58：Redis 不可用时 3 个进程各 2 次、间隔 0.2 秒，旧实现 0.2 秒就放行 6 次。"""
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(3) as pool:
        stamps = sorted(t for result in pool.map(_gate_worker, [(str(tmp_path), 2)] * 3) for t in result)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 6 and min(gaps) >= 0.19 and stamps[-1] - stamps[0] >= 0.95


def test_rate_gate_redis_failure_falls_back_to_the_shared_file_gate(tmp_path, monkeypatch):
    class Broken:
        def set(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setenv("DATAFABRIC_RATE_DIR", str(tmp_path))
    monkeypatch.setattr(rate, "_redis", lambda: Broken())
    calls = []
    monkeypatch.setattr(rate, "_wait_file", lambda g, i, d: calls.append(g))
    rate.wait("g", 0.5)
    assert calls == ["g"]


def test_rate_gate_respects_the_deadline(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setenv("DATAFABRIC_RATE_DIR", str(tmp_path))
    monkeypatch.delenv("DATAFABRIC_HISTORY_REDIS_URL", raising=False)
    rate._redis.cache_clear()
    rate.wait("slow", 30)
    with pytest.raises(TimeoutError):
        rate.wait("slow", 30, deadline=_time.monotonic() + 1)


def test_every_known_host_maps_to_its_catalog_rate_group():
    assert rate.group_for_url("https://clinicaltrials.gov/api/v2/studies").source_id == "clinicaltrials"
    assert rate.group_for_url("https://www.ebi.ac.uk/europepmc/x").source_id == "europe_pmc"
    assert rate.group_for_url("https://data.sec.gov/api/xbrl/x").min_interval_seconds == 1.0
    assert rate.group_for_url("https://api.fda.gov/x").source_id == "fda_crl"  # 最长后缀优先
    assert rate.group_for_url("https://eutils.ncbi.nlm.nih.gov/x").source_id == "pubmed"
    assert rate.group_for_url("https://unknown.example/x") is None


# ——登记差分 v2：只有可证明不影响判断的变化才不送模型——


BASE = {
    "trial_id": "NCT01234567",
    "status": "RECRUITING",
    "phase": ["PHASE3"],
    "enrollment": 400,
    "enrollment_type": "ESTIMATED",
    "primary_completion_date": "2026-06",
    "primary_completion_date_type": "ESTIMATED",
    "start_date": "2024-01-15",
    "design_info": {"allocation": "RANDOMIZED", "maskingInfo": {"masking": "DOUBLE"}},
    "eligibility": {"minimumAge": "18 Years", "sex": "ALL"},
    "arm_groups": [{"label": "High dose", "description": "XYZ-101 10 mg daily"}],
    "interventions": [{"type": "DRUG", "name": "XYZ-101"}, {"type": "DRUG", "name": "Placebo"}],
    "locations": [{"facility": "A", "city": "X", "contacts": [{"name": "Dr A", "phone": "1"}]}],
    "official_title": "A Phase 3 Study of XYZ-101",
    "has_results": False,
}
LOC = {"status": {"json_pointer": "/protocolSection/statusModule/overallStatus"}}


def changed(**new):
    after = {**BASE, **new}
    return {c["field"]: c for c in rd.diff(BASE, after, LOC)}


def test_identical_versions_have_no_changes():
    assert rd.diff(BASE, dict(BASE)) == []


@pytest.mark.parametrize(
    "case",
    [
        {"design_info": {"allocation": "NON_RANDOMIZED", "maskingInfo": {"masking": "DOUBLE"}}},  # 随机化取消
        {"eligibility": {"minimumAge": "6 Years", "sex": "ALL"}},  # 入组年龄放宽到儿童
        {"arm_groups": [{"label": "High dose", "description": "XYZ-101 100 mg daily"}]},  # 同名组剂量 10→100
        {"start_date": "2024-02-01"},  # 任何日期移动都不能证明无关
        {"primary_completion_date": "2026-07"},
        {"locations": [{"facility": "A", "city": "X"}, {"facility": "B", "city": "Y"}]},  # 新增地点
    ],
)
def test_codex_r58_counterexamples_and_any_unproven_change_go_to_the_model(case):
    field = next(iter(case))
    c = changed(**case)[field]
    assert c["material"] is True and c["material_reason"] == "not_provably_immaterial"


def test_same_named_arm_content_change_is_flagged_for_reading_the_original():
    c = changed(arm_groups=[{"label": "High dose", "description": "XYZ-101 100 mg daily"}])["arm_groups"]
    assert c["detail"]["same_names_content_changed"] is True
    assert "需读原件" in rd.describe(c)


def test_only_whitelisted_changes_skip_the_model():
    contacts = changed(locations=[{"facility": "A", "city": "X", "contacts": [{"name": "Dr B", "phone": "2"}]}])
    assert contacts["locations"]["material"] is False
    assert contacts["locations"]["material_reason"] == "site_contacts_only"
    title = changed(official_title="a phase 3  study of XYZ-101")
    assert title["official_title"]["material"] is False
    order = changed(interventions=[{"type": "DRUG", "name": "Placebo"}, {"type": "DRUG", "name": "XYZ-101"}])
    assert order["interventions"]["material"] is False


def test_field_appearing_or_disappearing_is_never_whitelisted():
    after = {k: v for k, v in BASE.items() if k != "locations"}
    c = {x["field"]: x for x in rd.diff(after, BASE)}["locations"]
    assert c["material"] is True and c["old_value"] == {"present": False}


def test_missing_old_value_stays_unknown_not_zero():
    """Codex r58 B：旧版原件没有地点字段，不能描述成“地点数 0 → 4”。"""
    after = {k: v for k, v in BASE.items() if k != "locations"}
    c = {x["field"]: x for x in rd.diff(after, BASE)}["locations"]
    assert c["detail"]["count_before"] is None
    text = rd.describe(c)
    assert "地点数 未知 → 1" in text and "0 →" not in text


def test_summary_change_outside_projected_facts_goes_to_the_model():
    """Codex r58 A：只改 briefSummary，facts 相同，也要送模型。"""
    out = rd.diff(BASE, dict(BASE), old_summary="Original summary", new_summary="Updated summary: new development")
    assert [(c["field"], c["material"]) for c in out] == [("brief_summary", True)]
    assert out[0]["json_pointer"].endswith("briefSummary")


def test_raw_change_outside_everything_projected_is_unmapped_and_material():
    out = rd.diff(BASE, dict(BASE), old_summary="s", new_summary="s", old_raw_stable='{"a":1}', new_raw_stable='{"a":2}')
    assert [(c["change_kind"], c["material"]) for c in out] == [("unmapped_change", True)]
    assert "需读两版原件" in rd.describe(out[0])
    # 原件相同（只差来源每次改写的数据日期，已在 stable_raw 里去掉）就没有变化
    assert rd.diff(BASE, dict(BASE), old_raw_stable='{"a":1}', new_raw_stable='{"a":1}') == []


def test_status_change_keeps_pointer_and_both_values_and_no_verdict():
    c = changed(status="TERMINATED", why_stopped="Sponsor decision")
    assert c["status"]["material"] and c["status"]["json_pointer"].endswith("overallStatus")
    assert c["status"]["old_value"] == {"present": True, "value": "RECRUITING"}
    assert c["why_stopped"]["old_value"] == {"present": False}
    assert "TERMINATED" in rd.describe(c["status"]) and "失败" not in rd.describe(c["status"])


def test_date_detail_and_long_values():
    d = changed(primary_completion_date="2027-03")["primary_completion_date"]["detail"]
    assert d == {"days": 273, "precision": "month", "direction": "later"}
    long_outcomes = [{"measure": "m" * 100, "timeFrame": str(i)} for i in range(40)]
    stored = changed(results_outcome_measures={"outcomeMeasures": long_outcomes})["results_outcome_measures"]["new_value"]
    assert stored["present"] and "sha256" in stored and "value" not in stored


def test_change_id_is_stable_and_versioned():
    assert rd.PROCESSOR_VERSION.endswith(".v2")
    a = rd.change_id("a" * 64, "b" * 64, "status")
    assert a == rd.change_id("a" * 64, "b" * 64, "status") and a != rd.change_id("a" * 64, "c" * 64, "status")
