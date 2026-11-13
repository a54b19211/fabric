"""质量段（clean/quality_stage.py）在真实 PostgreSQL 上（隔离 schema）：画像过的版本打一次分、对齐结果按版本存、
重跑不重复；清洗正文不变；/clean-text 不加 quality 时返回和以前一样。药名用本地白名单查，不联网。"""

import json
from datetime import datetime, timezone

from integration_clean_stage import rows
from integration_history import pg, pg_engine  # noqa: F401  -- 隔离库夹具

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
    assert not lines["approval_number"]["found"]  # 国药准字不是 FDA 申请号：格式校验不过，照实记
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
