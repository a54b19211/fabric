"""来源目录、定时刷新（配置、增量窗口、一个周期怎么读完）、限速和登记差分处理器；不联网、不连库。"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from clean import registry_diff as rd
from fetch import catalog, rate, source_refresh
from fetch.source_refresh import GIVE_UP_AFTER, SourceWatch, _key, _segment_done, _step, watches
from fetch.windows import KNOWN_STOP, SEGMENTS_PER_CYCLE, cycle_request
from store.history_models import FetchRequest, HistoryError, canonical, sha256

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def ct_watch(**extra):
    return SourceWatch.model_validate(
        {"id": "ct", "request": {"source_id": "clinicaltrials", "query": "*", "max_records": 100}, **extra}
    )


def pubmed_watch():
    return SourceWatch.model_validate({"id": "p", "request": {"source_id": "pubmed", "query": "BRCA1"}})


# ——来源目录——


STOP_AT_KNOWN = {
    "pubmed", "chictr", "nmpa", "cde", "cde_xxgk", "cde_zdyz", "phirda_industry", "phirda_policy", "phirda_rd",
    "phirda_healthcare_reform", "fda_press", "fda_drug_safety", "fda_api_drug_label", "fda_api_enforcement",
    "fiercepharma", "roche", "hkex", "nccn",  # 最后两个分组读：一家公司、一份指南一组
}


def test_watchable_set_is_the_earlier_five_and_the_newest_first_lists():
    # 改造前写死的四个来源，加上按整点读取的 GDELT，再加列表从新到旧、靠“遇到已存的就停”增量读的来源；
    # 港交所、NCCN 分组读，也进；本地导入不对外抓，不进
    earlier = {"who_don", "europe_pmc", "clinicaltrials", "sec_companyfacts", "gdelt_doc"}
    assert catalog.WATCHABLE == earlier | STOP_AT_KNOWN | {"fda_crl"}
    assert {s for s in catalog.WATCHABLE if catalog.spec(s).incremental.kind == "stop_at_known"} == STOP_AT_KNOWN
    assert catalog.spec("fda_crl").incremental.kind == "full_reread"  # 成批公布的旧信会排在读过的新信后面：整份重读
    assert {s for s in catalog.WATCHABLE if catalog.spec(s).incremental.by_group} == {"hkex", "nccn"}
    assert "local_files" in catalog.FETCHABLE and "local_files" not in catalog.WATCHABLE


def test_blocked_and_declared_sources_are_visible_gaps_not_fetchable():
    gaps = {g["source_id"]: g for g in catalog.gaps()}
    for source in ("cde_ctr", "cninfo", "biorxiv", "drugs_fda"):
        assert source in gaps and source not in catalog.FETCHABLE
    assert "脚本挑战" in gaps["cde_ctr"]["reason"]
    # 站点插件采集的来源：可按需读取，但不进固定刷新、标为未经本系统实测
    for source in catalog.SITE_SOURCES:
        assert source in catalog.FETCHABLE and source not in gaps and not catalog.spec(source).verified
    with pytest.raises(ValueError):
        FetchRequest.model_validate({"source_id": "cde_ctr", "query": "x"})


def test_every_spec_declares_decision_and_rate_group():
    for spec in catalog.SPECS:
        assert spec.decisions and set(spec.decisions) <= set("ABCDE")
        assert spec.rate_group and spec.min_interval_seconds > 0


# ——定时刷新的配置：状态键不变，配置按来源目录检查——


def test_a_watch_keeps_the_state_key_it_had_before_the_rewrite():
    """The state row's key is the sha256 of the config with its defaults filled in, computed as the scheduler before
    the rewrite did (FetchRequest's fields without known_stop): a running watch goes on with its saved state."""
    legacy = {"id": "ct", "cadence_seconds": 3600, "lookback_days": 30,
              "request": {"source_id": "clinicaltrials", "query": "*", "source_record_ids": [], "max_records": 100,
                          "max_pages": 3, "deadline_seconds": 60, "updated_since": None, "updated_until": None,
                          "continuation": None}}
    assert _key(ct_watch()) == "source-watch:" + sha256(canonical(legacy))


def test_a_watch_config_is_checked_against_the_catalog(monkeypatch):
    def watch(source, seconds=3600, **request):
        return {"id": "w", "cadence_seconds": seconds, "request": {"source_id": source, "query": "*", **request}}

    assert SourceWatch.model_validate(watch("who_don", 900)).cadence_seconds == 900  # WHO's own floor is 15 minutes
    for bad in (watch("who_don", 899), watch("clinicaltrials", 900),  # under the source's floor
                watch("local_files"),  # no incremental key: not watchable
                watch("pubmed", known_stop=3)):  # filled by each cycle, not written in the config
        with pytest.raises(ValueError):
            SourceWatch.model_validate(bad)
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps([watch("who_don"), watch("pubmed")]))
    with pytest.raises(HistoryError):  # the same id twice
        watches()


def test_known_stop_is_filled_by_the_cycle_for_newest_first_lists_only():
    pubmed = pubmed_watch()
    request, start, end = cycle_request(pubmed, {}, NOW)
    assert request["known_stop"] == KNOWN_STOP and request["query"] == "BRCA1" and start is end is None  # no window
    again, _, _ = cycle_request(pubmed, {"window_checkpoint": NOW.isoformat()}, NOW)
    assert again["known_stop"] == KNOWN_STOP and again.get("updated_since") is None  # a checkpoint changes nothing here
    assert cycle_request(ct_watch(), {}, NOW)[0]["known_stop"] == 0  # a source with an update window is read by it
    listed = SourceWatch.model_validate(
        {"id": "l", "request": {"source_id": "pubmed", "query": "x", "source_record_ids": ["1", "2"]}})
    assert cycle_request(listed, {}, NOW)[0]["known_stop"] == 0  # an explicit list is read to its end
    crl = SourceWatch.model_validate({"id": "c", "request": {"source_id": "fda_crl", "query": "*"}})
    request, start, end = cycle_request(crl, {}, NOW)
    assert request["known_stop"] == 0 and start is end is None  # the whole list every cycle, no window, no stop


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


def test_fixed_ids_have_no_window():
    sec = SourceWatch.model_validate({"id": "sec", "request": {
        "source_id": "sec_companyfacts", "query": "*", "source_record_ids": ["CIK0000012345:us-gaap:Revenues"],
        "max_pages": 1, "max_records": 1}})
    assert cycle_request(sec, {}, NOW)[1] is None


def test_europe_pmc_and_who_windows():
    epmc = SourceWatch.model_validate({"id": "e", "request": {"source_id": "europe_pmc", "query": "CRISPR"}})
    request, _, _ = cycle_request(epmc, {"window_checkpoint": "2026-11-03T00:00:00+00:00"}, NOW)
    assert request["query"] == "(CRISPR) AND UPDATE_DATE:[2026-11-01 TO 2026-11-04]"
    who = SourceWatch.model_validate({"id": "w", "request": {"source_id": "who_don", "query": "*"}})
    request, start, end = cycle_request(who, {}, NOW)
    assert request["updated_since"] == start.isoformat() and request["updated_until"] == end.isoformat()


def test_gdelt_cycle_reads_whole_utc_hours_from_the_checkpoint_without_overlap():
    gdelt = SourceWatch.model_validate({"id": "g", "request": {"source_id": "gdelt_doc", "query": "outbreak"}})
    now = datetime(2026, 11, 4, 12, 40, tzinfo=timezone.utc)
    request, start, end = cycle_request(gdelt, {}, now)  # no checkpoint: the lookback, from the hour mark
    assert start == datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    assert request["updated_since"] == start.isoformat() and request["updated_until"] == end.isoformat()
    _, start, end = cycle_request(gdelt, {"window_checkpoint": "2026-11-04T10:30:00+00:00"}, now)
    assert (start.hour, start.minute, end.hour, end.minute) == (10, 30, 12, 0)  # exactly the checkpoint
    _, start, end = cycle_request(gdelt, {"window_checkpoint": "2026-11-04T12:00:00+00:00"}, now)
    assert (start.hour, end.hour) == (11, 12)  # no new full hour yet: never an empty window


# ——一个周期：一段一段读完；出错换新任务重读这一段；读不到的下个周期整份读——


MORE = {"next_continuation": {"cursor": "c", "read_count": 100}}


def test_a_segment_with_a_cursor_reads_on_and_the_end_of_the_list_ends_the_cycle():
    state = {"request": {"known_stop": 0}, "window_ended_at": NOW.isoformat()}
    _segment_done(ct_watch(), state, MORE, NOW)
    assert state["state"] == "running" and state["request"]["continuation"] == MORE["next_continuation"]
    assert "window_checkpoint" not in state
    _segment_done(ct_watch(), state, {"upstream_pagination_exhausted": True}, NOW)
    assert state["state"] == "idle" and state["window_checkpoint"] == NOW.isoformat() and state["last_success_at"]
    assert "request" not in state and state["next_check_at"] == (NOW + timedelta(hours=1)).isoformat()
    known = {"request": {"known_stop": KNOWN_STOP}}
    _segment_done(pubmed_watch(), known, {"stop_reason": "reached_known"}, NOW)  # came to the stored ones
    assert known["state"] == "idle" and known["last_success_at"] and "window_checkpoint" not in known


def test_a_segment_that_did_not_move_or_stopped_short_without_a_cursor_is_a_failure():
    stalled = ({"request": {"continuation": MORE["next_continuation"]}}, MORE, "source_watch_no_progress")
    cut_short = ({"request": {}}, {"upstream_pagination_exhausted": False, "stop_reason": "page_budget"},
                 "source_watch_no_cursor")
    for state, coverage, code in (stalled, cut_short):
        with pytest.raises(HistoryError) as exc:
            _segment_done(ct_watch(), state, coverage, NOW)
        assert exc.value.code == code


def test_a_newest_first_cycle_reads_a_few_segments_a_grouped_one_all_its_groups_a_window_to_its_end():
    state = {"request": {"known_stop": KNOWN_STOP}, "segments": SEGMENTS_PER_CYCLE - 2}
    _segment_done(pubmed_watch(), state, MORE, NOW)
    assert state["state"] == "running"
    _segment_done(pubmed_watch(), state, {"next_continuation": {"cursor": "d", "read_count": 200}}, NOW)
    assert state["state"] == "idle" and state["last_success_at"]  # the older ones are ops/backfill_source.py's
    hkex = SourceWatch.model_validate({"id": "h", "request": {"source_id": "hkex", "query": "records"}})
    for watch in (hkex, ct_watch()):
        far = {"request": {"known_stop": 0}, "segments": SEGMENTS_PER_CYCLE + 20}
        _segment_done(watch, far, MORE, NOW)
        assert far["state"] == "running"


def test_a_cycle_with_unread_items_is_followed_by_one_full_read():
    unread = {"unread_items": [{"url": "https://x.invalid/9", "reason": "HTTP 403"}]}
    state = {"request": {"known_stop": KNOWN_STOP}}
    _segment_done(pubmed_watch(), state, {**MORE, **unread}, NOW)
    _segment_done(pubmed_watch(), state, {"stop_reason": "reached_known"}, NOW)
    assert state["read_all"] is True  # the unread one may sit behind stored ones: the next cycle does not stop at them
    assert cycle_request(pubmed_watch(), state, NOW)[0]["known_stop"] == 0
    full = {"request": {"known_stop": 0}}  # that full read leaves it unread again (a dead link): no second one
    _segment_done(pubmed_watch(), full, {"stop_reason": "items_unread", **unread}, NOW)
    assert full["read_all"] is False and full["state"] == "idle" and full["last_success_at"]
    hkex = SourceWatch.model_validate({"id": "h", "request": {"source_id": "hkex", "query": "records"}})
    assert cycle_request(hkex, {"read_all": True}, NOW)[0]["known_stop"] == KNOWN_STOP  # each group stops on its own


def stub_runs(monkeypatch, runs):
    """store.history 和队列的桩：create_fetch 记下请求，依次给出 runs 里的任务；read_run 给出最近建的那个。"""
    calls = {"created": [], "enqueued": [], "failed": []}

    def create_fetch(request):
        calls["created"].append(request)
        return runs[len(calls["created"]) - 1], True

    monkeypatch.setattr(source_refresh.history, "create_fetch", create_fetch)
    monkeypatch.setattr(source_refresh.history, "read_run", lambda run_id: runs[len(calls["created"]) - 1])
    monkeypatch.setattr(source_refresh.history, "fail_job", lambda *args: calls["failed"].append(args))
    monkeypatch.setattr(source_refresh.fetch_job, "enqueue_fetch", lambda run: calls["enqueued"].append(run))
    monkeypatch.setattr(source_refresh.publication, "disk_status", lambda: {"backfill_paused": False})
    return calls


def a_run(state, **coverage):
    error = {"code": "source_read_failed", "message": "x"} if state == "failed" else None
    return SimpleNamespace(id="r-" + state, attempt=1, state=state, created_at=NOW, error=error, coverage=coverage)


def test_a_failed_segment_is_read_again_in_full_and_three_failures_give_the_cycle_up(monkeypatch):
    calls = stub_runs(monkeypatch, [a_run("failed")] * 3)
    state = {}
    for _ in range(GIVE_UP_AFTER):
        _step(pubmed_watch(), state, NOW)
        assert state["state"] == "failed" and state["error"]["code"] == "source_watch_failed"
    # the retries do not stop at stored records: the failed try may have stored the first ones of the segment
    assert [request.known_stop for request in calls["created"]] == [KNOWN_STOP, 0, 0]
    assert "request" not in state and state["read_all"] is True  # given up: the next cycle starts again, in full
    assert state["next_check_at"] == (NOW + timedelta(hours=1)).isoformat()


def test_a_queued_run_goes_on_the_queue_and_one_left_by_a_dead_worker_is_failed_and_replaced(monkeypatch):
    calls = stub_runs(monkeypatch, [a_run("queued")])
    state = {}
    _step(ct_watch(), state, NOW)
    assert state["state"] == "running" and state["active_run_id"] == "r-queued" and len(calls["enqueued"]) == 1
    _step(ct_watch(), state, NOW + timedelta(hours=1))  # more than 3 x JOB_TIMEOUT later, still queued
    assert calls["failed"] and state["state"] == "failed" and "active_run_id" not in state
    assert state["request"] and state["window_ended_at"] == NOW.isoformat()  # the same window, by a new run


def _gate_worker(args):
    import os
    import time as _time

    directory, n = args
    os.environ["DATAFABRIC_RATE_DIR"] = directory
    from fetch import rate as gate

    stamps = []
    for _ in range(n):
        gate.wait("probe.example", 0.2)
        stamps.append(_time.time())
    return stamps


def test_rate_gate_is_shared_across_processes(tmp_path):
    """Codex r58：3 个进程各 2 次、间隔 0.2 秒，各算各的话 0.2 秒就放行 6 次。"""
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(3) as pool:
        stamps = sorted(t for result in pool.map(_gate_worker, [(str(tmp_path), 2)] * 3) for t in result)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 6 and min(gaps) >= 0.19 and stamps[-1] - stamps[0] >= 0.95


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
    assert title["official_title"]["material_reason"] == "title_case_or_spacing_only"
    order = changed(interventions=[{"type": "DRUG", "name": "Placebo"}, {"type": "DRUG", "name": "XYZ-101"}])
    assert order["interventions"]["material_reason"] == "order_only"


def test_codex_r61_case_is_only_equivalent_where_it_really_is():
    """单位 mL→ML、描述大小写都不能证明等价；只有正式题目可以忽略大小写。"""
    units = changed(results_outcome_measures={"unitOfMeasure": "ML"})
    base = {**BASE, "results_outcome_measures": {"unitOfMeasure": "mL"}}
    c = {x["field"]: x for x in rd.diff(base, {**base, "results_outcome_measures": {"unitOfMeasure": "ML"}})}
    assert c["results_outcome_measures"]["material"] is True
    arm = changed(arm_groups=[{"label": "High dose", "description": "xyz-101 10 MG daily"}])
    assert arm["arm_groups"]["material"] is True
    assert units  # 从缺到有也是变化


def test_codex_r61_unmapped_change_is_not_hidden_by_a_minor_change():
    """题目只改大小写（白名单）+ 详细描述（投影外）改了：后者照样送模型。"""
    import json as _json

    old_raw = _json.dumps({"protocolSection": {"descriptionModule": {"detailedDescription": "old"},
                                               "statusModule": {"lastUpdateSubmitDate": "2026-01-01"}}})
    new_raw = _json.dumps({"protocolSection": {"descriptionModule": {"detailedDescription": "new safety signal"},
                                               "statusModule": {"lastUpdateSubmitDate": "2026-02-01"}}})
    out = {c["field"]: c for c in rd.diff(BASE, {**BASE, "official_title": "A PHASE 3 STUDY OF XYZ-101"},
                                          old_raw_stable=old_raw, new_raw_stable=new_raw)}
    assert out["official_title"]["material"] is False
    assert out["_unmapped"]["material"] is True
    assert out["_unmapped"]["detail"]["changed_paths"] == ["/protocolSection/descriptionModule/detailedDescription"]


def test_bookkeeping_dates_alone_are_not_a_content_change():
    import json as _json

    old_raw = _json.dumps({"protocolSection": {"statusModule": {"lastUpdatePostDateStruct": {"date": "2026-01-01"},
                                                                "statusVerifiedDate": "2026-01"}}})
    new_raw = _json.dumps({"protocolSection": {"statusModule": {"lastUpdatePostDateStruct": {"date": "2026-02-01"},
                                                                "statusVerifiedDate": "2026-02"}}})
    assert rd.diff(BASE, dict(BASE), old_raw_stable=old_raw, new_raw_stable=new_raw) == []


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
    assert rd.PROCESSOR_VERSION.endswith(".v3")
    a = rd.change_id("a" * 64, "b" * 64, "status")
    assert a == rd.change_id("a" * 64, "b" * 64, "status") and a != rd.change_id("a" * 64, "c" * 64, "status")
