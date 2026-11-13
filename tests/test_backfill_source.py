"""ops/backfill_source.py：什么时候停、怎么说明。读一段（read_leg）换成替身，不连库、不联网。"""

import sys
from types import SimpleNamespace

import pytest

from ops import backfill_source


def run_with(monkeypatch, capsys, args, legs):
    """按 args 跑一遍命令，每一段依次给出 legs 里的 (state, coverage)；返回 (退出码, 打印的文字, 各段收到的任务键摘要)。"""
    monkeypatch.setenv("DATAFABRIC_HISTORY_DATABASE_URL", "postgresql://unused.invalid/none")
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", "")
    monkeypatch.setattr(sys, "argv", ["backfill_source.py", *args])
    tags, answers = [], iter(legs)

    def read_leg(source, query, continuation, leg, deadline, tag):
        tags.append(tag)
        state, coverage = next(answers)
        return SimpleNamespace(state=state, coverage=coverage, error=None)

    monkeypatch.setattr(backfill_source, "read_leg", read_leg)
    code = backfill_source.main()
    return code, capsys.readouterr().out, tags


def leg(new=0, same=0, cursor=None, **more):
    cont = {"scope_hash": "0" * 64, "cursor": cursor, "read_count": 0} if cursor else None
    return "partial", {"new_version_count": new, "same_version_count": same, "next_continuation": cont, **more}


def test_a_leg_that_does_not_move_the_cursor_and_reads_nothing_stops_the_command(monkeypatch, capsys):
    code, out, _ = run_with(monkeypatch, capsys, ["roche"], [leg(5, 0, "c1"), leg(0, 0, "c1"), leg(1, 0, "c2")])
    assert code == 1 and "续读位置没有前进" in out and "第 3 段" not in out
    assert "新增 5" in out  # 累计还在


def test_a_leg_that_moved_on_without_new_versions_is_still_progress(monkeypatch, capsys):
    code, out, _ = run_with(monkeypatch, capsys, ["roche"], [leg(0, 3, "c1"), leg(0, 3, "c2"),
                                                            ("completed", {"upstream_pagination_exhausted": True})])
    assert code == 0 and "读到底了" in out


def test_pages_overrides_the_configured_pages_of_a_list_site_and_the_end_says_how_far_it_read(monkeypatch, capsys):
    from fetch.site_profiles import SITE_PROFILES

    monkeypatch.setitem(SITE_PROFILES["chictr"], "max_pages", 50)
    done = ("completed", {"upstream_pagination_exhausted": True})
    code, out, _ = run_with(monkeypatch, capsys, ["chictr"], [done])
    assert code == 0 and "只读到站点配置的页数（50 页" in out
    code, out, _ = run_with(monkeypatch, capsys, ["chictr", "--pages", "12000"], [done])
    assert SITE_PROFILES["chictr"]["max_pages"] == 12000 and "12000 页" in out
    with pytest.raises(SystemExit):  # 接口站点没有页数可改
        run_with(monkeypatch, capsys, ["roche", "--pages", "5"], [done])


def test_the_deadline_is_five_to_a_hundred_and_twenty_seconds_and_belongs_to_the_leg_key(monkeypatch, capsys):
    done = ("completed", {"upstream_pagination_exhausted": True})
    for bad in ("4", "121"):
        with pytest.raises(SystemExit):
            run_with(monkeypatch, capsys, ["roche", "--deadline", bad], [done])
    capsys.readouterr()
    tags = {run_with(monkeypatch, capsys, ["roche", "--deadline", seconds], [done])[2][0] for seconds in ("120", "60")}
    assert len(tags) == 2  # 换了 deadline，任务键不同：不会和已有的运行撞键
