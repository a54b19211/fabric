"""v3 3.3 清洗段在真实 PostgreSQL 上（隔离 schema）：跨类型最小样本——登记结构化（CT）、论文叙述
（Europe PMC，含缺摘要）、站点页面（NMPA，含空页）、附件（解析成功/失败）。不联网、不调模型。"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import text

from biz_rules.history_models import ArchiveRecord, FetchRequest
from contracts import data_objects as do
from processing import clean_text
from db import history as store
from integration_history import (  # noqa: F401  -- 复用隔离库夹具和发布入口
    _fetch_attachment,
    _fetch_europe_pmc_raw,
    _fetch_study,
    pg,
    pg_engine,
)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def allow(monkeypatch):
    monkeypatch.setenv(
        "DATAFABRIC_HISTORY_SOURCE_ALLOWLIST",
        '["external_archive","clinicaltrials","pubmed","fda_crl","europe_pmc","nmpa"]',
    )


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


def test_every_source_type_gets_clean_text_identifiers_and_a_profile(pg, monkeypatch, tmp_path):
    from db import clean_stage

    allow(monkeypatch)
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
    lit = clean_stage.for_versions("europe_pmc", False, [paper], "current", NOW())["versions"][paper]
    assert "No significant" in lit["clean"]["text"] and "≥3×ULN" in lit["clean"]["text"]
    assert lit["profile"]["doc_type"] == "primary_research"
    notice = clean_stage.for_versions("nmpa", False, [page], "current", NOW())["versions"][page]
    assert "首页 > 公告" not in notice["clean"]["text"] and "未见安全性问题" in notice["clean"]["text"]
    assert [i["value"] for i in notice["identifiers"]] == ["国药准字H20234567"]
    # 论文摘要里写了 NCT 号：和 CT 登记按同一编号关联（提及，不是同一份材料）
    links = clean_stage.identifier_links(paper)
    assert {"version_id": ct, "source_id": "clinicaltrials", "kind": "nct", "value": "NCT09990011",
            "relation": "mentions_record"} in links
    assert clean_stage.process(batch=50)["profiled"] == 0  # 幂等


def test_short_and_empty_material_are_kept_and_routed_honestly(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
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
    from db import clean_stage, derived_text
    from processing import attachment_parse as ap

    allow(monkeypatch)
    good, _ = _fetch_attachment(tmp_path, monkeypatch, "cs-att-ok")
    bad, _ = _fetch_attachment(tmp_path, monkeypatch, "cs-att-bad", content=b"%PDF-1.4 other bytes")
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


def test_stored_clean_text_fits_the_data_contract(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    ct = _fetch_study("NCT09990012", "cs-contract").version_ids[0]
    clean_stage.process(batch=50)
    got = clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW())["versions"][ct]
    obj = do.from_clean_text({**got["clean"], "version_id": ct}, source_id="clinicaltrials",
                             record_id=got["source_record_id"], seen=got["first_seen_at"],
                             proof=got.get("proof_available_at"))
    assert isinstance(obj, do.CleanText) and obj.inputs[0].version_id == ct
    assert obj.locators[0].kind == "json_pointer"


def test_derived_tables_are_append_only(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    _fetch_study("NCT09990013", "cs-append")
    clean_stage.process(batch=50)
    assert rows(pg, "SELECT count(*) AS n FROM derived_profiles")[0]["n"] == 1
    with pg.begin() as conn:
        assert conn.execute(text("DELETE FROM derived_profiles")).rowcount == 0
        assert conn.execute(text("DELETE FROM derived_identifiers")).rowcount == 0


def test_a_cleaner_upgrade_recomputes_identifiers_and_profile_and_keeps_old_rows(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    ct = _fetch_study("NCT09990014", "cs-upgrade").version_ids[0]
    clean_stage.process(batch=50)
    monkeypatch.setattr(clean_text, "PROCESSOR_VERSION", "synthetic.clean_text.v9")
    assert clean_stage.process(batch=50)["profiled"] == 1  # 新清洗器版本：重算
    got = clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW())["versions"][ct]
    assert got["clean"]["processor_version"] == "synthetic.clean_text.v9"
    assert got["profile"]["text_processor_version"] == "synthetic.clean_text.v9"
    assert rows(pg, "SELECT count(*) AS n FROM derived_profiles WHERE version_id=:v", v=ct)[0]["n"] == 2  # 旧行保留


# ——规模化运行方式：分块认领、按输入复用、死信、水位（主持人：海量数据要够硬）——


def test_same_input_is_cleaned_once_and_reused(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    calls = []
    real = clean_text.clean_literature
    monkeypatch.setattr(clean_text, "clean_literature", lambda raw: calls.append(raw) or real(raw))
    record = {"source": "MED", "id": "123", "title": "Same", "abstractText": "No change in the abstract."}
    _fetch_europe_pmc_raw(json.dumps(record), "reuse-1")
    _fetch_europe_pmc_raw(json.dumps({**record, "citedByCount": 7}), "reuse-2")  # 元数据变了，摘要没变
    counts = clean_stage.process(chunk_seqs=1)
    # 第二个版本只改了引用数：清洗器只读标题和摘要，输入相同，复用，不再清洗
    assert counts["profiled"] == 2 and counts["cleaned"] == 1 and counts["reused"] == 1 and len(calls) == 1


def test_identical_cleaner_input_on_another_version_is_reused(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    page_a = nmpa_page("# 公告\n\n同一份正文，未见异常。", "reuse-page-a")
    page_b = nmpa_page("# 公告\n\n同一份正文，未见异常。", "reuse-page-b")  # 另一个栏目地址，正文相同
    counts = clean_stage.process()
    assert counts["cleaned"] == 1 and counts["reused"] == 1
    a = clean_stage.for_versions("nmpa", False, [page_a, page_b], "current", NOW())["versions"]
    assert a[page_a]["clean"]["text"] == a[page_b]["clean"]["text"]


def test_two_workers_never_process_the_same_chunk(pg, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from db import clean_stage

    allow(monkeypatch)
    for i in range(12):
        _fetch_study(f"NCT0999{i:04d}", f"conc-{i}")
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: clean_stage.process(chunk_seqs=2), range(3)))
    assert sum(r["profiled"] for r in results) == 12
    assert rows(pg, "SELECT count(*) AS n FROM derived_profiles")[0]["n"] == 12
    assert rows(pg, "SELECT count(DISTINCT version_id) AS n FROM derived_clean_text")[0]["n"] == 12


def test_an_abandoned_chunk_is_reclaimed_after_its_lease(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    _fetch_study("NCT09998881", "lease-1")
    chunk = clean_stage._claim(pg, clean_stage.stage_key(), 10, 60)  # 认领后进程“死了”
    assert chunk and clean_stage.process()["profiled"] == 0  # 租约未到期：别人不碰
    with pg.begin() as conn:
        conn.execute(text("UPDATE derived_stage_chunks SET lease_until=clock_timestamp()-interval '1 second'"))
    assert clean_stage.process()["profiled"] == 1


def test_a_failing_chunk_is_dead_lettered_with_its_reason_and_retried_a_bounded_number_of_times(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    _fetch_study("NCT09998882", "fail-1")
    monkeypatch.setattr(clean_text, "clean_registry", lambda *a, **k: 1 / 0)
    for _ in range(clean_stage.MAX_CHUNK_ATTEMPTS + 2):
        clean_stage.process()
    [chunk] = rows(pg, "SELECT state, attempts, error FROM derived_stage_chunks")
    assert chunk["state"] == "failed" and chunk["attempts"] == clean_stage.MAX_CHUNK_ATTEMPTS
    assert chunk["error"]["type"] == "ZeroDivisionError"
    status = clean_stage.stage_status()
    ct = next(s for s in status["sources"] if s["source_id"] == "clinicaltrials")
    assert ct["failed"] == 1 and ct["lag"] == 1  # 失败不推进水位，积压如实可见


def test_watermark_and_lag(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    for i in range(5):
        _fetch_study(f"NCT0997{i:04d}", f"wm-{i}")
    clean_stage.process(chunk_seqs=2, max_chunks=2)
    ct = next(s for s in clean_stage.stage_status()["sources"] if s["source_id"] == "clinicaltrials")
    assert ct["published_seq"] == 5 and ct["watermark"] == 4 and ct["lag"] == 1
    clean_stage.process(chunk_seqs=2)
    ct = next(s for s in clean_stage.stage_status()["sources"] if s["source_id"] == "clinicaltrials")
    assert ct["watermark"] == 5 and ct["lag"] == 0 and ct["versions_per_second"]


def test_registry_summary_comes_from_the_original_brief_summary(pg, monkeypatch):
    """CT 投影在没有摘要时用标题顶替；清洗只认原件 briefSummary：有摘要不报 summary_missing。"""
    from db import clean_stage

    allow(monkeypatch)
    ct = _fetch_study("NCT09990014", "cs-summary", summary="A phase 3 study of drug X.").version_ids[0]
    bare = _fetch_study("NCT09990015", "cs-no-summary").version_ids[0]
    clean_stage.process(batch=50)
    got = clean_stage.for_versions("clinicaltrials", False, [ct, bare], "current", NOW())["versions"]
    assert "summary_missing" not in (got[ct]["clean"]["warnings"] or [])
    assert "A phase 3 study of drug X." in got[ct]["clean"]["text"]
    assert "summary_missing" in got[bare]["clean"]["warnings"]  # 没有摘要就如实报缺，不拿标题顶替


# ——Codex r66 的两个反例（原样逻辑，断言写成验收）——
def test_last_expired_lease_becomes_a_dead_letter_and_can_be_retried(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    _fetch_study("NCT09998883", "codex-last-lease")
    key = clean_stage.stage_key()
    for _ in range(clean_stage.MAX_CHUNK_ATTEMPTS):
        assert clean_stage._claim(pg, key, 10, 60)
        with pg.begin() as conn:
            conn.execute(text("UPDATE derived_stage_chunks SET lease_until=clock_timestamp()-interval '1 second' "
                              "WHERE stage_key=:k"), {"k": key})
    clean_stage.process(max_chunks=1)
    [chunk] = rows(pg, "SELECT state, attempts, error FROM derived_stage_chunks")
    assert chunk["state"] == "failed" and chunk["error"]["type"] == "lease_exhausted"
    [src] = [s for s in clean_stage.stage_status()["sources"] if s["source_id"] == "clinicaltrials"]
    assert src["dead_letter"] == 1 and src["leased"] == 0 and src["lag"] == src["published_seq"]
    assert clean_stage.retry_failed() == 1
    assert clean_stage.process()["chunks"] == 1  # 受控重置后重新认领并完成
    assert rows(pg, "SELECT state FROM derived_stage_chunks")[0]["state"] == "done"


def test_unready_waiting_attachment_does_not_starve_a_ready_one(pg, monkeypatch, tmp_path):
    from collections import defaultdict
    from pathlib import Path

    from db import clean_stage, derived_text
    from processing import attachment_parse

    allow(monkeypatch)
    first, path1 = _fetch_attachment(tmp_path, monkeypatch, "codex-waiting-first", content=b"%PDF-1.4 synthetic blocked")
    second, path2 = _fetch_attachment(tmp_path, monkeypatch, "codex-waiting-second", content=b"%PDF-1.4 synthetic ready")
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


def test_profile_business_categories_are_stored_and_returned(pg, monkeypatch):
    from db import clean_stage

    allow(monkeypatch)
    ct = _fetch_study("NCT09990016", "cs-categories").version_ids[0]
    clean_stage.process()
    prof = clean_stage.for_versions("clinicaltrials", False, [ct], "current", NOW())["versions"][ct]["profile"]
    assert prof["doc_type"] == "registry_record" and prof["doc_category"] == "registry"
    assert "registry" in prof["evidence_categories"]
