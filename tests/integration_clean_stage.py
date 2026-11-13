"""v3 3.3 清洗段在真实 PostgreSQL 上（隔离 schema）：跨类型最小样本——登记结构化（CT）、论文叙述
（Europe PMC，含缺摘要）、站点页面（NMPA，含空页）、附件（解析成功/失败）。不联网、不调模型。"""

import hashlib
import json
from datetime import datetime, timezone

import pytest
from integration_history import (  # noqa: F401  -- 复用隔离库夹具和发布入口
    _fetch_attachment,
    _fetch_europe_pmc_raw,
    _fetch_study,
    pg,
    pg_engine,
)
from sqlalchemy import text

from clean import clean_text
from store import history as store
from store.history_models import ArchiveRecord, FetchRequest, HistoryError

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def rows(pg, sql, **params):
    with pg.begin() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def nmpa_page(markdown, key):
    url = "https://www.nmpa.gov.cn/xxgk/ggtg/" + key + ".html"
    raw = json.dumps({"schema_version": "datafabric.web_capture.v1", "role": "web_capture", "site": "nmpa", "url": url,
                      "html_sha256": "0" * 64, "html": "<html></html>",
                      "parsed": {"title": "公告", "publish_date": "2026-11-01", "markdown": markdown,
                                 "parse_method": "css_extract", "extra": {}},
                      "attachments": []}, ensure_ascii=False, sort_keys=True)
    run, _ = store.create_fetch(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[url]), key)
    store.job_input(run.id, run.attempt)
    return store.finish_fetch(run.id, run.attempt, {
        "records": [ArchiveRecord(source_record_id=url, record_kind="document", raw_content=raw)],
        "complete": False, "upstream_pagination_exhausted": True, "stop_reason": "upstream_exhausted",
        "requests": 1, "bytes": len(raw), "source_total": None, "error": None,
    }).version_ids[0]


def test_every_source_type_gets_clean_text_identifiers_and_a_profile(pg, tmp_path):
    from clean import clean_stage

    ct = _fetch_study("NCT09990011", "cs-ct").version_ids[0]
    paper = _fetch_europe_pmc_raw(json.dumps({
        "source": "MED", "id": "123", "title": "XYZ-101 in NASH",
        "abstractText": "No significant hepatotoxicity was observed (ALT ≥3×ULN in 2.1%; NCT09990011).",
        "pubTypeList": {"pubType": ["research-article", "Journal Article"]}}), "cs-epmc").version_ids[0]
    page = nmpa_page("# 关于注销药品注册证书的公告\n\n首页 > 公告\n\n国药准字H20234567 的注册证书予以注销，"
                     "未见安全性问题。", "cs-page")
    assert clean_stage.process(batch=50)["profiled"] == 3
    got = clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW())["versions"][ct]
    assert got["clean"]["processor_version"] == clean_text.PROCESSOR_VERSION
    assert any(s["source"].get("json_pointer") for s in got["clean"]["segments"])
    assert {"kind": "nct", "value": "NCT09990011"} in [{k: i[k] for k in ("kind", "value")} for i in got["identifiers"]]
    assert got["profile"]["doc_type"] == "registry_record" and got["profile"]["route"] in ("normal", "weak_signal")
    # v3 6.1：按引用里绑定的处理器版本回读；那一版没有产物就是没有，不拿当前版冒充
    named = clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW(),
                                     processor_version=clean_text.PROCESSOR_VERSION)["versions"][ct]
    assert named["clean"]["text"] == got["clean"]["text"]
    older = clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW(),
                                     processor_version="datafabric.clean_text.v1")["versions"][ct]
    assert older["state"] == "not_processed" and "clean" not in older
    with pytest.raises(HistoryError):
        clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW(), processor_version="x;drop")
    lit = clean_stage.for_versions("europe_pmc", False, [paper], "current", NOW())["versions"][paper]
    # v3 6.2: stored and returned with the profile (experiment type candidates, obtainable reading level)
    assert got["profile"]["experiment_types"] == [{"type": "human", "basis": "registry_record", "locators": []}]
    assert got["profile"]["reading_level"]["level"] == "registry_record"
    assert lit["profile"]["reading_level"]["level"] == "abstract"
    assert {e["type"] for e in lit["profile"]["experiment_types"]} == {"unknown"}
    assert "No significant" in lit["clean"]["text"] and "≥3×ULN" in lit["clean"]["text"]
    assert lit["profile"]["doc_type"] == "primary_research"
    notice = clean_stage.for_versions("nmpa", False, [page], "current", NOW())["versions"][page]
    assert "首页 > 公告" not in notice["clean"]["text"] and "未见安全性问题" in notice["clean"]["text"]
    assert [i["value"] for i in notice["identifiers"]] == ["国药准字H20234567"]
    assert clean_stage.process(batch=50)["profiled"] == 0  # 幂等


def test_short_and_empty_material_are_kept_and_routed_honestly(pg):
    from clean import clean_stage

    title_only = _fetch_europe_pmc_raw(json.dumps({"source": "MED", "id": "123", "title": "Case report"}),
                                       "cs-short").version_ids[0]
    empty = nmpa_page("首页 > 公告\n\n打印本页", "cs-empty")
    clean_stage.process(batch=50)
    short = clean_stage.for_versions("europe_pmc", False, [title_only], "current", NOW())["versions"][title_only]
    assert short["profile"]["route"] == "weak_signal"
    assert short["profile"]["quality"]["format"]["state"] == "degraded"
    blank = clean_stage.for_versions("nmpa", False, [empty], "current", NOW())["versions"][empty]
    assert blank["profile"]["route"] == "quarantine" and blank["clean"]["text"] == ""
    assert blank["profile"]["doc_type"] == "regulatory_notice"  # 元数据照常保留


def test_attachments_are_profiled_on_their_parsed_text_and_failed_ones_quarantined(pg, monkeypatch, tmp_path):
    from clean import attachment_parse as ap
    from clean import clean_stage, derived_text

    good, _ = _fetch_attachment(tmp_path, "cs-att-ok")
    bad, _ = _fetch_attachment(tmp_path, "cs-att-bad", content=b"%PDF-1.4 other bytes")
    assert clean_stage.process(batch=50)["waiting_for_attachment_text"] == 2  # 还没解析：等，不画像
    answers = {"ok": {"success": True, "markdown": "受理号 CXHL2300123 的审评结论：不予批准。", "error": ""}}

    def parse(path):
        return answers["ok"] if hashlib.sha256(open(path, "rb").read()).hexdigest() == hashlib.sha256(
            b"%PDF-1.4 synthetic notice").hexdigest() else {"success": False, "markdown": "", "error": "x"}

    monkeypatch.setattr(ap, "_mineru", parse)
    for _ in range(derived_text.MAX_ATTEMPTS):
        derived_text.process(batch=10, retry_after_seconds=0)
    clean_stage.process(batch=50)
    ok = clean_stage.for_versions("nmpa", False, [good], "current", NOW())["versions"][good]
    assert ok["clean"]["processor_version"] == ap.PROCESSOR_VERSION  # 附件正文来自加工层，不另清洗一份
    assert [i["value"] for i in ok["identifiers"]] == ["CXHL2300123"]
    assert ok["profile"]["doc_type"] == "attachment"
    failed = clean_stage.for_versions("nmpa", False, [bad], "current", NOW())["versions"][bad]
    assert failed["profile"]["route"] == "quarantine"


def test_derived_tables_are_append_only(pg):
    from clean import clean_stage

    _fetch_study("NCT09990013", "cs-append")
    clean_stage.process(batch=50)
    assert rows(pg, "SELECT count(*) AS n FROM derived_profiles")[0]["n"] == 1
    with pg.begin() as conn:
        assert conn.execute(text("DELETE FROM derived_profiles")).rowcount == 0
        assert conn.execute(text("DELETE FROM derived_identifiers")).rowcount == 0


def test_a_cleaner_upgrade_recomputes_identifiers_and_profile_and_keeps_old_rows(pg, monkeypatch):
    from clean import clean_stage

    ct = _fetch_study("NCT09990014", "cs-upgrade").version_ids[0]
    clean_stage.process(batch=50)
    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "synthetic.clean_text.v9")
    assert clean_stage.process(batch=50)["profiled"] == 1  # 新清洗器版本：重算
    got = clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW())["versions"][ct]
    assert got["clean"]["processor_version"] == "synthetic.clean_text.v9"
    assert got["profile"]["text_processor_version"] == "synthetic.clean_text.v9"
    assert rows(pg, "SELECT count(*) AS n FROM derived_profiles WHERE version_id=:v", v=ct)[0]["n"] == 2  # 旧行保留


# ——规模化运行方式：SKIP LOCKED 领活、洗过的不重洗、失败不挡别的版本、进度——


def test_a_version_already_cleaned_keeps_its_text_when_only_the_profile_is_redone(pg, monkeypatch):
    from clean import clean_stage, profile

    calls = []
    real = clean_text.clean_literature
    monkeypatch.setattr(clean_text, "clean_literature", lambda raw: calls.append(raw) or real(raw))
    record = {"source": "MED", "id": "123", "title": "Same", "abstractText": "No change in the abstract."}
    _fetch_europe_pmc_raw(json.dumps(record), "reuse-1")
    assert clean_stage.process()["cleaned"] == 1
    monkeypatch.setattr(profile, "PROCESSOR_VERSION", "synthetic.profile.v99")  # only the profile is redone
    counts = clean_stage.process()
    assert (counts["profiled"], counts["cleaned"], counts["reused"], len(calls)) == (1, 0, 1, 1)
    [row] = rows(pg, "SELECT text_sha256, input_sha256 FROM derived_clean_text")
    assert len(row["text_sha256"]) == 64 and row["input_sha256"] is None  # one digest per text, no input digest


def test_two_workers_never_process_the_same_version(pg):
    from concurrent.futures import ThreadPoolExecutor

    from clean import clean_stage

    for i in range(12):
        _fetch_study(f"NCT0999{i:04d}", f"conc-{i}")
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: clean_stage.process(batch=2), range(3)))
    assert sum(r["profiled"] for r in results) == 12
    assert rows(pg, "SELECT count(*) AS n FROM derived_profiles")[0]["n"] == 12
    assert rows(pg, "SELECT count(DISTINCT version_id) AS n FROM derived_clean_text")[0]["n"] == 12


def test_a_version_locked_by_another_worker_is_skipped_until_it_lets_go(pg):
    from clean import clean_stage

    _fetch_study("NCT09998881", "lock-1")
    with pg.connect() as other:  # another worker holds the version
        other.begin()
        other.execute(text("SELECT 1 FROM history_versions FOR NO KEY UPDATE"))
        assert clean_stage.process()["profiled"] == 0
        other.rollback()  # it stopped without writing anything: the lock goes with its transaction
    assert clean_stage.process()["profiled"] == 1


def test_a_failing_version_is_reported_does_not_hold_up_the_others_and_is_tried_again(pg, monkeypatch):
    from clean import clean_stage

    bad = _fetch_study("NCT09998882", "fail-1", summary="Synthetic summary the cleaner trips on.").version_ids[0]
    _fetch_study("NCT09998883", "fail-2")
    real = clean_text.clean_registry

    def fails_on_one(facts, *args, **kwargs):
        if kwargs.get("summary") == "Synthetic summary the cleaner trips on.":
            raise ZeroDivisionError("synthetic cleaner fault")
        return real(facts, *args, **kwargs)

    monkeypatch.setattr(clean_text, "clean_registry", fails_on_one)
    counts = clean_stage.process()
    assert counts["profiled"] == 1 and counts["failed_versions"] == [
        {"version_id": bad, "error": "ZeroDivisionError: synthetic cleaner fault"}]
    assert clean_stage.process()["failed_versions"][0]["version_id"] == bad  # tried again the next round
    monkeypatch.setattr(clean_text, "clean_registry", real)
    assert clean_stage.process()["profiled"] == 1


def test_stage_status_counts_what_is_done_and_what_is_left(pg):
    from clean import clean_stage

    for i in range(5):
        _fetch_study(f"NCT0997{i:04d}", f"wm-{i}")

    def ct():
        return next(s for s in clean_stage.stage_status()["sources"] if s["source_id"] == "clinicaltrials")

    assert (ct()["published_seq"], ct()["versions"], ct()["profiled"], ct()["todo"]) == (5, 5, 0, 5)
    clean_stage.process(batch=2)
    assert (ct()["profiled"], ct()["waiting"], ct()["todo"]) == (5, 0, 0)


def test_registry_summary_comes_from_the_original_brief_summary(pg):
    """CT 投影在没有摘要时用标题顶替；清洗只认原件 briefSummary：有摘要不报 summary_missing。"""
    from clean import clean_stage

    ct = _fetch_study("NCT09990014", "cs-summary", summary="A phase 3 study of drug X.").version_ids[0]
    bare = _fetch_study("NCT09990015", "cs-no-summary").version_ids[0]
    clean_stage.process(batch=50)
    got = clean_stage.for_versions("clinicaltrials", False, [ct, bare], "current", NOW())["versions"]
    assert "summary_missing" not in (got[ct]["clean"]["warnings"] or [])
    assert "A phase 3 study of drug X." in got[ct]["clean"]["text"]
    assert "summary_missing" in got[bare]["clean"]["warnings"]  # 没有摘要就如实报缺，不拿标题顶替


# ——Codex r66 的反例（原样逻辑，断言写成验收）——
def test_unready_waiting_attachment_does_not_starve_a_ready_one(pg, monkeypatch, tmp_path):
    from collections import defaultdict
    from pathlib import Path

    from clean import attachment_parse, clean_stage, derived_text

    first, path1 = _fetch_attachment(tmp_path, "codex-waiting-first", content=b"%PDF-1.4 synthetic blocked")
    second, path2 = _fetch_attachment(tmp_path, "codex-waiting-second", content=b"%PDF-1.4 synthetic ready")
    assert clean_stage.process()["waiting_for_attachment_text"] == 2
    head = rows(pg, "SELECT version_id FROM derived_stage_waiting WHERE stage_key=:k "
                    "ORDER BY created_at, version_id LIMIT 1", k=clean_stage.stage_key())[0]["version_id"]
    ready, ready_path = (second, path2) if head == first else (first, path1)
    monkeypatch.setattr(attachment_parse, "_mineru", lambda p: {
        "success": True, "markdown": "The decision for NCT09998883 remains unchanged.", "error": ""}
        if Path(p).read_bytes() == ready_path.read_bytes()
        else {"success": False, "markdown": "", "error": "synthetic parser pending"})
    parsed = derived_text.process(batch=10, retry_after_seconds=0)
    assert parsed["parsed"] == 1 and parsed["failed"] == 1
    counts = defaultdict(int)
    clean_stage._process_waiting(pg, clean_stage.stage_key(), counts, limit=1)
    assert rows(pg, "SELECT version_id FROM derived_profiles WHERE version_id=:v", v=ready)
    left = [r["version_id"] for r in rows(pg, "SELECT version_id FROM derived_stage_waiting")]
    assert left == [head]  # 没解析完的还在等，已解析的已补做


def test_profile_business_categories_are_stored_and_returned(pg):
    from clean import clean_stage

    ct = _fetch_study("NCT09990016", "cs-categories").version_ids[0]
    clean_stage.process()
    prof = clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW())["versions"][ct]["profile"]
    assert prof["doc_type"] == "registry_record" and prof["doc_category"] == "registry"
    assert "registry" in prof["evidence_categories"]
