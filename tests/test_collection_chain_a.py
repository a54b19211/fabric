"""唯一采集框架（来源目录、增量窗口检查点、实时/回填）和登记差分处理器；不联网、不连库。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from clean import registry_diff as rd
from fetch import catalog, rate
from fetch.source_refresh import SourceWatch, _key, _page_finished, watches
from fetch.windows import cycle_request
from store.history_models import FetchRequest, HistoryError

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def ct_watch(**extra):
    return SourceWatch.model_validate(
        {"id": "ct", "request": {"source_id": "clinicaltrials", "query": "*", "max_records": 100}, **extra}
    )


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


def test_every_spec_declares_decision_rate_group_and_incremental_semantics():
    for spec in catalog.SPECS:
        assert spec.decisions and set(spec.decisions) <= set("ABCDE")
        assert spec.rate_group and spec.min_interval_seconds > 0
        if spec.watchable:
            assert spec.incremental.semantics and spec.incremental.kind != "none"


def test_read_public_refuses_unconnected_plugin(monkeypatch):
    from fetch import sources

    monkeypatch.setitem(sources.PLUGINS, "who_don", None)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    request = FetchRequest.model_validate(
        {"source_id": "who_don", "query": "*", "updated_since": "2026-11-01T00:00:00Z"}
    )
    with pytest.raises(HistoryError) as exc:
        sources.read_public(request)
    assert exc.value.code == "source_plugin_missing"


# ——调度：实时/回填、上限、旧键不变——


def test_existing_realtime_watch_keeps_its_state_key():
    from store.history_models import canonical, sha256

    watch = ct_watch()
    legacy = watch.model_dump(mode="json")
    legacy.pop("mode")
    legacy["request"].pop("known_stop")  # the field that came later does not move the key of a running watch
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


def test_cadence_floor_is_each_sources_own_who_don_runs_every_15_minutes():
    def watch(source, seconds):
        return SourceWatch.model_validate(
            {"id": "w", "cadence_seconds": seconds, "request": {"source_id": source, "query": "*"}}
        )

    assert watch("who_don", 900).cadence_seconds == 900 and watch("clinicaltrials", 3600).cadence_seconds == 3600
    for source, seconds in (("who_don", 899), ("clinicaltrials", 900)):
        with pytest.raises(ValueError):
            watch(source, seconds)


def test_a_source_without_an_incremental_key_cannot_be_watched(monkeypatch):
    for source in ("local_files",):
        monkeypatch.setenv(
            "DATAFABRIC_HISTORY_WATCHES", json.dumps([{"id": "p", "request": {"source_id": source, "query": "records"}}])
        )
        with pytest.raises(HistoryError):
            watches()


def test_known_stop_is_filled_by_the_cycle_not_written_in_the_watch(monkeypatch):
    from fetch.windows import KNOWN_STOP

    pubmed = SourceWatch.model_validate({"id": "p", "request": {"source_id": "pubmed", "query": "BRCA1"}})
    request, start, end = cycle_request(pubmed, {}, NOW)
    assert request["known_stop"] == KNOWN_STOP and request["query"] == "BRCA1" and start is end is None  # no window
    again, _, _ = cycle_request(pubmed, {"window_checkpoint": NOW.isoformat()}, NOW)
    assert again["known_stop"] == KNOWN_STOP and again.get("updated_since") is None  # a checkpoint changes nothing here
    clinical = cycle_request(ct_watch(), {}, NOW)[0]
    assert clinical["known_stop"] == 0  # a source with an update window is read by the window, as before
    listed = SourceWatch.model_validate(
        {"id": "l", "request": {"source_id": "pubmed", "query": "x", "source_record_ids": ["1", "2"]}})
    assert cycle_request(listed, {}, NOW)[0]["known_stop"] == 0  # an explicit list is read to its end
    crl = SourceWatch.model_validate({"id": "c", "request": {"source_id": "fda_crl", "query": "*"}})
    request, start, end = cycle_request(crl, {}, NOW)
    assert request["known_stop"] == 0 and start is end is None  # the whole list every cycle, no window, no stop
    written = [{"id": "p", "request": {"source_id": "pubmed", "query": "x", "known_stop": 3}}]
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps(written))
    with pytest.raises(HistoryError):
        watches()


def test_a_read_that_came_to_known_records_ends_the_cycle_and_a_failed_page_is_read_in_full_next_time():
    pubmed = SourceWatch.model_validate({"id": "p", "request": {"source_id": "pubmed", "query": "BRCA1"}})
    state = {"request": {"known_stop": 3}}
    _page_finished(pubmed, state, {"stop_reason": "reached_known", "upstream_pagination_exhausted": False}, NOW)
    assert state["state"] == "idle" and state["last_success_at"] and "request" not in state
    from fetch.source_refresh import _read_all_next_time

    state = {"request": {"known_stop": 3, "continuation": {"cursor": "c"}}}
    _read_all_next_time(state)  # the page failed or left items unread: the retry may not stop at the first known ones
    assert state["request"] == {"known_stop": 0, "continuation": {"cursor": "c"}}


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


def test_gdelt_cycle_reads_whole_utc_hours_from_the_checkpoint_without_overlap(monkeypatch):
    values = [{"id": "g", "request": {"source_id": "gdelt_doc", "query": "outbreak"}}]
    monkeypatch.setenv("DATAFABRIC_HISTORY_WATCHES", json.dumps(values))
    [gdelt] = watches()  # GDELT is watchable now
    now = datetime(2026, 11, 4, 12, 40, tzinfo=timezone.utc)
    request, start, end = cycle_request(gdelt, {}, now)  # no checkpoint: the lookback, from the hour mark
    assert start == datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    assert request["updated_since"] == start.isoformat() and request["updated_until"] == end.isoformat()
    _, start, end = cycle_request(gdelt, {"window_checkpoint": "2026-11-04T10:30:00+00:00"}, now)
    assert (start.hour, start.minute, end.hour, end.minute) == (10, 30, 12, 0)  # exactly the checkpoint
    _, start, end = cycle_request(gdelt, {"window_checkpoint": "2026-11-04T12:00:00+00:00"}, now)
    assert (start.hour, end.hour) == (11, 12)  # no new full hour yet: never an empty window


def test_page_finished_pages_on_ends_at_the_end_and_still_rejects_a_stop_short_without_receipts():
    paging = {"request": {}, "window_ended_at": NOW.isoformat()}
    _page_finished(ct_watch(), paging, {"next_continuation": {"cursor": "c"}}, NOW)
    assert paging["state"] == "paging" and paging["request"]["continuation"] == {"cursor": "c"}
    assert "window_checkpoint" not in paging
    done = {"request": {}, "window_ended_at": NOW.isoformat(), "cycle_pages": 1}
    _page_finished(ct_watch(), done, {"upstream_pagination_exhausted": True}, NOW)
    assert done["state"] == "idle" and done["window_checkpoint"] == NOW.isoformat() and done["last_success_at"]
    assert "request" not in done and done["cycle_pages"] == 2
    stopped = {"request": {}, "window_ended_at": NOW.isoformat()}  # no cursor, no receipts: not a finished read
    with pytest.raises(HistoryError) as exc:
        _page_finished(ct_watch(), stopped, {"upstream_pagination_exhausted": False, "stop_reason": "page_budget"}, NOW)
    assert exc.value.code == "source_watch_no_cursor"


def test_a_newest_first_cycle_ends_after_a_fixed_number_of_segments_and_a_stalled_cursor_is_a_failure():
    from fetch.windows import SEGMENTS_PER_CYCLE

    pubmed = SourceWatch.model_validate({"id": "p", "request": {"source_id": "pubmed", "query": "BRCA1"}})
    more = {"next_continuation": {"cursor": "c", "read_count": 100}, "upstream_pagination_exhausted": False}
    state = {"request": {}, "cycle_pages": SEGMENTS_PER_CYCLE - 2}  # one more is still read on
    _page_finished(pubmed, state, more, NOW)
    assert state["state"] == "paging" and state["request"]["continuation"] == more["next_continuation"]
    state["cycle_pages"] = SEGMENTS_PER_CYCLE - 1  # the last segment of the cycle: done, the older ones are the backfill's
    _page_finished(pubmed, state, more, NOW)
    assert state["state"] == "idle" and state["last_success_at"] and "request" not in state
    # a source read by its update window is not cut off: the window has to be read to its end
    window = {"request": {}, "window_ended_at": NOW.isoformat(), "cycle_pages": 50}
    _page_finished(ct_watch(), window, more, NOW)
    assert window["state"] == "paging"
    # the cursor did not move and the segment read nothing: that is not progress
    stalled = {"request": {"continuation": more["next_continuation"]}}
    with pytest.raises(HistoryError) as exc:
        _page_finished(pubmed, stalled, {**more, "adopted_count": 0}, NOW)
    assert exc.value.code == "source_watch_no_progress"
    moved = {"request": {"continuation": {"cursor": "c", "read_count": 0}}}
    _page_finished(pubmed, moved, {**more, "adopted_count": 100}, NOW)
    assert moved["state"] == "paging"


def test_a_grouped_cycle_reads_all_its_groups_and_a_failed_segment_is_read_in_full_only_once():
    from fetch.source_refresh import _read_all_next_time
    from fetch.windows import KNOWN_STOP, SEGMENTS_PER_CYCLE

    hkex = SourceWatch.model_validate({"id": "h", "request": {"source_id": "hkex", "query": "records"}})
    more = {"next_continuation": {"cursor": "c", "read_count": 3}, "upstream_pagination_exhausted": False}
    state = {"request": {"known_stop": KNOWN_STOP}, "cycle_pages": SEGMENTS_PER_CYCLE + 20}  # 远过 5 段，照样读下去
    _page_finished(hkex, state, more, NOW)
    assert state["state"] == "paging" and state["request"]["continuation"] == more["next_continuation"]
    _read_all_next_time(state)  # 一段读失败了：重读这一段不许停在已存的（前面几条可能是失败的那次存下的）
    assert state["request"]["known_stop"] == 0
    _page_finished(hkex, state, {**more, "next_continuation": {"cursor": "d", "read_count": 9}}, NOW)
    assert state["request"]["known_stop"] == KNOWN_STOP  # 下一段又按公司停在已存的，不是整个周期都整读
    done = {"request": {"known_stop": KNOWN_STOP}, "cycle_pages": 30}
    _page_finished(hkex, done, {"upstream_pagination_exhausted": True}, NOW)
    assert done["state"] == "idle" and done["last_success_at"]  # 读完全部公司，周期收工
    # 上一周期有公司读不到：下个周期不整份重读（每家公司的“已存就停”互不相干，读不到的下个周期自然再读）
    assert cycle_request(hkex, {"read_all": True}, NOW)[0]["known_stop"] == KNOWN_STOP
    pubmed = SourceWatch.model_validate({"id": "p", "request": {"source_id": "pubmed", "query": "BRCA1"}})
    assert cycle_request(pubmed, {"read_all": True}, NOW)[0]["known_stop"] == 0  # 一份有顺序的列表才整份重读


def _step(monkeypatch, watch, state, *, run=None, disk_low=False):
    """refresh_once 里的一步：_advance，出错交给 _failed。库和队列换成桩：run 是这一步读到的、已读完的那次读取
    （没有 run 就是还没建），disk_low 为真时磁盘在低水位。"""
    from contextlib import nullcontext
    from types import SimpleNamespace

    from fetch import source_refresh

    monkeypatch.setattr(source_refresh.history, "history_engine", lambda: SimpleNamespace(begin=nullcontext))
    monkeypatch.setattr(source_refresh.history, "_source", lambda permit, source_id: None)
    monkeypatch.setattr(source_refresh.history, "read_run", lambda run_id: run)
    monkeypatch.setattr(source_refresh.publication, "disk_status", lambda: {"backfill_paused": disk_low})
    if run is not None:
        state["active_run_id"] = "r1"  # 失败后换新 run 重试：桩每次给的是同一个读完的结果
    try:
        source_refresh._advance(None, "key", watch, state, NOW)
    except HistoryError as exc:
        source_refresh._failed(watch, state, exc, NOW)


def _read(cursor, adopted):
    from types import SimpleNamespace

    coverage = {"next_continuation": cursor, "adopted_count": adopted}
    return SimpleNamespace(state="completed", error=None, coverage=coverage)


def test_three_stalled_reads_in_a_row_give_the_cycle_up_and_a_page_read_cleanly_ends_the_streak(monkeypatch):
    """同一个位置一直原地不动（PubMed 翻过 1 万条之后）：不能每个间隔都重试同一个位置，连续 3 次之后放弃这一周期；
    中间读成功一页，连续次数从头数。走的是真实路径：_advance → _page_finished 抛错 → _failed。"""
    pubmed = SourceWatch.model_validate({"id": "p", "request": {"source_id": "pubmed", "query": "BRCA1"}})
    stuck, moved = {"cursor": "9900", "read_count": 9900}, {"cursor": "10000", "read_count": 10000}
    state = {"request": {"known_stop": 3, "continuation": stuck}}
    for streak in (1, 2):
        _step(monkeypatch, pubmed, state, run=_read(stuck, 0))
        assert state["state"] == "failed" and state["fail_streak"] == streak and state["request"]["continuation"] == stuck
    _step(monkeypatch, pubmed, state, run=_read(moved, 100))
    assert state["state"] == "paging" and state["fail_streak"] == 0  # 读成功一页，连续失败数清零
    for streak in (1, 2):
        _step(monkeypatch, pubmed, state, run=_read(moved, 0))
        assert state["fail_streak"] == streak and state["request"]["continuation"] == moved
    _step(monkeypatch, pubmed, state, run=_read(moved, 0))
    assert "request" not in state and state["fail_streak"] == 0 and state["read_all"] is True  # 下个间隔从头


def test_a_backfill_keeps_its_place_when_the_disk_is_low_or_the_reads_fail(monkeypatch):
    """回填的续读位置是读了很久才到的：磁盘低水位、许可撤回不是这次读取的问题，不计失败；读取本身连续失败 3 次，
    回填也不丢位置（丢了就从头读起），按间隔重试——放弃周期是实时刷新的办法。"""
    from types import SimpleNamespace

    cursor = {"cursor": "c", "read_count": 900}
    for mode in ("backfill", "realtime"):
        watch = ct_watch(mode=mode)
        state = {"request": {"continuation": cursor}}
        for _ in range(3):
            _step(monkeypatch, watch, state, disk_low=True)
        assert state["error"]["code"] == "source_watch_disk_low" and not state.get("fail_streak")
        assert state["request"]["continuation"] == cursor  # 磁盘低水位多少次都不丢
    broken = SimpleNamespace(state="failed", error={"code": "source_read_failed", "message": "x"}, coverage={})
    backfill = ct_watch(mode="backfill")
    state = {"request": {"continuation": cursor}}
    for _ in range(4):
        _step(monkeypatch, backfill, state, run=broken)
    assert state["fail_streak"] == 4 and state["request"]["continuation"] == cursor and state["state"] == "failed"
    realtime = ct_watch()
    state = {"request": {"continuation": cursor}}
    for _ in range(3):
        _step(monkeypatch, realtime, state, run=broken)
    assert "request" not in state and state["read_all"] is True  # 实时刷新：放弃这一周期，下个间隔重新规划


def test_a_read_that_ends_without_a_cursor_is_counted_and_read_again_until_the_cycle_is_given_up(monkeypatch):
    """一次读取正常结束却没给续读位置（来源截断、没有窗口回执）：不能每个间隔都取回同一个读完的结果、永远报同一个错。
    它按读取失败计数、换新 run 重读，连续 3 次实时刷新放弃这一周期，回填保留位置。走真实路径：_advance → _page_finished → _failed。"""
    from types import SimpleNamespace

    cut_short = SimpleNamespace(
        state="completed", error=None, coverage={"upstream_pagination_exhausted": False, "stop_reason": "page_budget"}
    )
    state = {"request": {"known_stop": 3}, "window_ended_at": NOW.isoformat()}
    for streak in (1, 2):
        _step(monkeypatch, ct_watch(), state, run=cut_short)
        assert state["state"] == "failed" and state["error"]["code"] == "source_watch_no_cursor"
        assert state["fail_streak"] == streak and "request" in state
        assert "active_run_id" not in state and "request_key" not in state  # 下个间隔换新 run 重读，不再取回这一个
    _step(monkeypatch, ct_watch(), state, run=cut_short)
    assert "request" not in state and state["fail_streak"] == 0 and state["read_all"] is True  # 下个间隔从头
    assert state["error"]["code"] == "source_watch_no_cursor"  # 来源健康（WATCH_FIELDS 的 error）里还看得到原因
    cursor = {"cursor": "c", "read_count": 900}
    backfill = {"request": {"continuation": cursor}, "window_ended_at": NOW.isoformat()}
    for _ in range(4):
        _step(monkeypatch, ct_watch(mode="backfill"), backfill, run=cut_short)
    assert backfill["fail_streak"] == 4 and backfill["request"]["continuation"] == cursor  # 回填不丢位置
    assert "active_run_id" not in backfill


def test_page_finished_at_a_ceiling_ends_the_cycle_with_its_reason_and_keeps_the_cursor():
    who = SourceWatch.model_validate(
        {"id": "w", "cadence_seconds": 900, "request": {"source_id": "who_don", "query": "*"}}
    )
    checkpoint = "2026-11-03T00:00:00+00:00"
    state = {"request": {}, "window_ended_at": NOW.isoformat(), "window_checkpoint": checkpoint}
    cursor = {"cursor": "c"}
    # pages were read, then the daily ceiling refused the next request: the cursor is kept, the cycle waits
    _page_finished(who, state, {"stop_reason": "outbound_quota_day", "next_continuation": cursor}, NOW)
    assert state["state"] == "idle" and state["request"]["continuation"] == cursor  # not paging: no retry at once
    assert state["window_checkpoint"] == checkpoint and "last_success_at" not in state
    assert state["next_check_at"] == (NOW + timedelta(seconds=900)).isoformat()
    # the next cycle asks for the same page and is refused again: still the same cursor, still no failure
    _page_finished(who, state, {"stop_reason": "outbound_quota_day"}, NOW)
    assert state["state"] == "idle" and state["request"]["continuation"] == cursor
    assert state["last_coverage"]["stop_reason"] == "outbound_quota_day"


def test_outbound_caps_are_the_declared_ones_and_count_per_site(mid_hour_clock):
    who, gdelt = catalog.spec("who_don"), catalog.spec("gdelt_doc")
    assert (who.min_interval_seconds, who.outbound_cap) == (30, catalog.OutboundCap(per_day=200))
    assert gdelt.min_interval_seconds == 6
    assert gdelt.outbound_cap == catalog.OutboundCap(per_day=900, per_hour_per_query=7)
    url = "https://www.who.int/api/hubs/diseaseoutbreaknews"
    assert all(rate.take_for_url(url) is None for _ in range(200))
    assert rate.take_for_url(url) == "outbound_quota_day"
    assert rate.take_for_url("https://clinicaltrials.gov/api/v2/studies") is None  # no cap declared: not counted


def _gate_worker(args):
    import os
    import time as _time

    directory, n = args
    os.environ["DATAFABRIC_RATE_DIR"] = directory
    os.environ.pop("DATAFABRIC_HISTORY_REDIS_URL", None)
    from fetch import rate as gate

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


def test_rate_gate_redis_failure_waits_and_never_splits_into_a_second_clock(monkeypatch):
    """Codex r61：配了 Redis 而它故障时不能切到文件闸门（会和仍连得上 Redis 的进程各算各的）。"""

    class Broken:
        def set(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(rate, "_redis", lambda: Broken())
    monkeypatch.setattr(rate, "_wait_file", lambda *a: pytest.fail("must not use the file gate"))
    monkeypatch.setattr(rate.time, "sleep", lambda s: None)
    import time as _time

    with pytest.raises(TimeoutError):
        rate.wait("g", 0.5, deadline=_time.monotonic() + 0.0001)
    with pytest.raises(TimeoutError, match="rate_gate_unavailable"):
        rate.wait("g", 0.5)


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


def _history_rows(monkeypatch, rows):
    """store.history.history_engine 的替身：stuck_items 的那条查询返回这些行。"""
    from contextlib import contextmanager

    from fetch import source_refresh

    class Conn:
        def execute(self, *args, **kwargs):
            return self

        def all(self):
            return rows

    class Engine:
        @contextmanager
        def begin(self):
            yield Conn()

    monkeypatch.setattr(source_refresh.history, "history_engine", lambda: Engine())


def _unread(*urls):
    return [{"url": url, "reason": "HTTP 403"} for url in urls]


def test_health_lists_the_items_unread_in_each_of_the_last_three_rounds(monkeypatch):
    from fetch import source_refresh

    # (读取范围, 是不是一轮的第一段, 这一段读不到的条目)，新的在前
    rows = [("A", True, _unread("u1", "u2")), ("A", True, _unread("u1")), ("A", True, _unread("u1", "u3")),
            ("A", True, _unread("u1")),
            ("B", True, _unread("u1")), ("B", True, _unread("u1")),  # B 只有两轮：不够
            ("C", True, _unread("u9")), ("C", True, None), ("C", True, _unread("u9"))]  # C 中间有一轮整次失败：连续数断了
    _history_rows(monkeypatch, rows)
    assert source_refresh.stuck_items("nmpa") == [{"url": "u1", "reason": "HTTP 403", "rounds": 3}]


def test_a_round_of_several_segments_counts_as_one_round_for_the_stuck_items(monkeypatch):
    from fetch import source_refresh

    # 每轮两段：u1 总在第二段（False = 不是第一段）。按段数连续三次是不对的：最近三段里只有一段有它
    rows = [("D", False, _unread("u1")), ("D", True, _unread("u2")), ("D", False, _unread("u1")), ("D", True, None),
            ("D", False, _unread("u1")), ("D", True, None)]
    _history_rows(monkeypatch, rows)
    assert source_refresh.stuck_items("nmpa") == [{"url": "u1", "reason": "HTTP 403", "rounds": 3}]


def test_a_cycle_with_unread_items_is_followed_by_a_cycle_that_reads_it_all_but_a_stuck_one_does_not_count(monkeypatch):
    from fetch import source_refresh

    pubmed = SourceWatch.model_validate({"id": "p", "request": {"source_id": "pubmed", "query": "BRCA1"}})
    monkeypatch.setattr(source_refresh, "stuck_items", lambda source: [])
    unread = {"unread_items": _unread("u9")}
    state = {"request": {}}
    _page_finished(pubmed, state, {"next_continuation": {"cursor": "c", "read_count": 100}, **unread}, NOW)
    assert state["state"] == "paging" and state["unclean"]  # a middle segment: the cycle goes on, the mark is kept
    _page_finished(pubmed, state, {"stop_reason": "reached_known"}, NOW)
    assert state["read_all"] is True and "unclean" not in state and "request" not in state
    source_refresh._plan_cycle(pubmed, state, NOW)
    assert state["request"]["known_stop"] == 0 and "read_all" not in state  # that cycle reads from the start, in full
    _page_finished(pubmed, state, {"stop_reason": "reached_known"}, NOW)
    assert state["read_all"] is False
    source_refresh._plan_cycle(pubmed, state, NOW)
    assert state["request"]["known_stop"] == 3  # and the one after a clean cycle stops at the known ones again
    # an item that is already in the stuck list (a dead link) does not ask for another full read
    monkeypatch.setattr(source_refresh, "stuck_items", lambda source: [{"url": "u9", "reason": "x", "rounds": 3}])
    dead = {"request": {}}
    _page_finished(pubmed, dead, {"stop_reason": "items_unread", **unread}, NOW)
    assert dead["read_all"] is False and dead["state"] == "idle" and dead["last_success_at"]
