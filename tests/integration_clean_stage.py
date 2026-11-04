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
    obj = clean_stage.to_contract(got, source_id="clinicaltrials")
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
