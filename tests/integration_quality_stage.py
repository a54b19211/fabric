"""质量段（clean/quality_stage.py）在真实 PostgreSQL 上（隔离 schema）：画像过的版本打一次分、对齐结果按版本存、
重跑不重复；清洗正文不变；/clean-text 不加 quality 时返回和以前一样。药名用本地白名单查，不联网。"""

import json
from datetime import datetime, timezone

from integration_clean_stage import rows
from integration_history import pg, pg_engine  # noqa: F401  -- 隔离库夹具
from sqlalchemy import text

from clean import align, clean_stage, quality, quality_stage
from store import history as store
from store.history_models import ArchiveRecord, FetchRequest

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731
TEXT = "# 关于暂停销售的公告\n\n" + "阿司匹林肠溶片（国药准字H20234567）暂停销售，原因见附件。" * 5


def nmpa_page(key, drug_name):
    """一个 NMPA 网页版本，站点抽取器在 extra 里给了药名（投影后在 facts.extra）。"""
    url = "https://www.nmpa.gov.cn/xxgk/ggtg/" + key + ".html"
    raw = json.dumps({"schema_version": "datafabric.web_capture.v1", "role": "web_capture", "site": "nmpa", "url": url,
                      "html": "<html></html>", "attachments": [],
                      "parsed": {"title": "关于暂停销售的公告", "publish_date": "2026-11-01", "markdown": TEXT,
                                 "parse_method": "css_extract",
                                 "extra": {"drug_name": drug_name, "approval_number": "国药准字H20234567"}}},
                     ensure_ascii=False, sort_keys=True)
    run, _ = store.create_fetch(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[url]), key)
    store.job_input(run.id, run.attempt)
    return store.finish_fetch(run.id, run.attempt, {
        "records": [ArchiveRecord(source_record_id=url, record_kind="document", raw_content=raw)],
        "complete": False, "upstream_pagination_exhausted": True, "stop_reason": "upstream_exhausted",
        "requests": 1, "bytes": len(raw), "source_total": None, "error": None,
    }).version_ids[0]


def test_a_profiled_version_is_scored_once_with_its_alignments_and_its_clean_text_is_untouched(pg):
    from api.routers.history import clean_text_route

    version = nmpa_page("qs-one", "Placebo; Aspirin Tablets 100 mg")
    clean_stage.process(batch=50)
    before = clean_stage.for_versions("nmpa", False, [version], "current", NOW())
    counts = quality_stage.process(ask=[align.local])
    assert (counts["scored"], counts["aligned"], counts["failed_versions"]) == (1, 1, [])
    score = rows(pg, "SELECT * FROM derived_quality_scores WHERE version_id = :v", v=version)
    assert len(score) == 1 and score[0]["scorer_version"] == quality.SCORER_VERSION
    assert set(score[0]["dimensions"]) == set(quality.WEIGHTS) and score[0]["level"] in ("高", "中", "低", "隔离")
    lines = {r["resolution_key"]: r for r in rows(pg, "SELECT * FROM derived_authority_resolutions WHERE version_id = :v",
                                                  v=version)}
    assert lines["Placebo"]["skipped"] and not lines["Placebo"]["found"]
    aspirin = lines["Aspirin Tablets 100 mg"]
    assert (aspirin["std_id"], aspirin["candidate"], aspirin["rule"]) == ("LOCAL:aspirin", "Aspirin", "cut_descriptor")
    assert (lines["approval_number"]["id_type"], lines["approval_number"]["std_id"]) == (
        "nmpa_approval", "NMPA:国药准字H20234567")  # NMPA 的批准文号按国药准字校验
    assert quality_stage.process(ask=[align.local])["scored"] == 0  # 重跑不重复
    # 雷达读的东西不变：清洗正文、不加 quality 的 /clean-text
    plain = clean_text_route("nmpa", False, "current", NOW(), [version])
    assert plain["versions"] == before["versions"] and "quality" not in plain["versions"][version]
    with_quality = clean_text_route("nmpa", False, "current", NOW(), [version], quality=True)["versions"][version]
    assert with_quality["quality"]["score"] == score[0]["score"]
    assert {a["resolution_key"] for a in with_quality["quality"]["alignments"]} == {
        "Placebo", "Aspirin Tablets 100 mg", "approval_number"}


def test_a_new_scorer_version_rescores_without_asking_the_authorities_again(pg, monkeypatch):
    version = nmpa_page("qs-rescore", "Aspirin Tablets 100 mg")
    clean_stage.process(batch=50)
    assert quality_stage.process(ask=[align.local])["scored"] == 1
    monkeypatch.setattr(quality, "SCORER_VERSION", "datafabric.quality.v999")

    def no_network(name):
        raise AssertionError("已对齐过的版本不应再查")

    counts = quality_stage.process(ask=[no_network])
    assert (counts["scored"], counts["alignment_reused"], counts["failed_versions"]) == (1, 1, [])
    assert len(rows(pg, "SELECT 1 FROM derived_quality_scores WHERE version_id = :v", v=version)) == 2  # 旧分数保留


def test_when_the_authorities_are_down_nothing_degraded_is_stored_and_the_round_goes_on(pg):
    without_names = nmpa_page("qs-plain", "")
    second = nmpa_page("qs-second", "Nivolumab")
    first = nmpa_page("qs-first", "Pembrolizumab")  # 最新的先做
    mine = [without_names, second, first]
    clean_stage.process(batch=50)
    asked = []

    def down(name):
        asked.append(name)
        return align.result(name, "pubchem", degraded=True)

    counts = quality_stage.process(ask=[down])
    # 第一个有药名的版本查不成：不存、当作断网；后一个有药名的留到下一轮，没有药名的照常打分
    assert [f["version_id"] for f in counts["failed_versions"]] == [first] and asked == ["Pembrolizumab"]
    assert (counts["offline"], counts["deferred"], counts["scored"]) == (True, 1, 1)
    assert rows(pg, "SELECT version_id FROM derived_quality_scores WHERE version_id = ANY(:ids)", ids=mine) == [
        {"version_id": without_names}]
    assert rows(pg, "SELECT 1 FROM derived_authority_resolutions WHERE degraded AND version_id = ANY(:ids)", ids=mine) == []


def test_an_instance_without_network_pages_past_a_full_page_of_versions_it_cannot_align(pg, monkeypatch):
    """不出网的实例只有本地白名单，有药名的版本永远存不成；它们占满一页时，翻到下一页，没有药名的照样打分。"""
    plain = nmpa_page("qs-page-plain", "")  # 最早的
    names = [nmpa_page("qs-page-" + str(i), "Nivolumab") for i in range(3)]
    clean_stage.process(batch=50)
    monkeypatch.setattr(quality_stage, "TODO_LIMIT", 2)
    monkeypatch.setenv("DATAFABRIC_HISTORY_FETCH_ENABLED", "false")
    counts = quality_stage.process(batch=1)
    assert counts["offline"] and counts["scored"] == 1 and counts["deferred"] == 2
    assert rows(pg, "SELECT version_id FROM derived_quality_scores WHERE version_id = ANY(:ids)",
                ids=[plain, *names]) == [{"version_id": plain}]


def test_a_version_another_process_is_scoring_is_skipped(pg):
    version = nmpa_page("qs-locked", "Aspirin Tablets 100 mg")
    clean_stage.process(batch=50)
    with pg.connect() as other:
        other.execute(text("SELECT 1 FROM derived_profiles WHERE version_id = :v FOR UPDATE"), {"v": version})
        assert quality_stage.process(ask=[align.local])["scored"] == 0  # 锁着：别的进程在做，跳过
        other.rollback()
    assert quality_stage.process(ask=[align.local])["scored"] == 1
