"""旧 ETL 的 NMPA / CDE / ChiCTR 采集并入唯一框架（collection.legacy_sites）。

不联网：页面抓取、解析和附件下载都换成替身；旧代码的站点配置和调用顺序原样复用。
"""

import json

import pytest

from biz_rules.history_models import ArchiveRecord, FetchRequest, HistoryError
from biz_rules.history_sources import PLUGINS, project_record, read_public
from collection import catalog, legacy_sites, rate

PAGE = "https://www.nmpa.gov.cn/xxgk/ggtg/20261101120000123.html"
ATT1 = "https://www.nmpa.gov.cn/directory/web/nmpa/images/a1.doc"
ATT2 = "https://www.nmpa.gov.cn/directory/web/nmpa/images/a2.pdf"


class FakePage:
    status = 200
    html_content = "<html><body><h1>关于某药品的公告</h1><a href='a1.doc'>附件1</a></body></html>"


@pytest.fixture
def fake_web(monkeypatch, tmp_path):
    calls = {"fetch": [], "download": []}
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)

    def fetch_page(url, **kwargs):
        calls["fetch"].append((url, kwargs))
        return {"page": FakePage(), "url": url, "uuid": "u-" + url[-8:], "timestamp": "t", "cookies": [{"name": "c"}]}

    def route_webpage(page_data, site_key=None):
        return {
            "uuid": page_data["uuid"],
            "title": "关于某药品的公告",
            "publish_date": "2026-11-01",
            "markdown": "# 关于某药品的公告\n\n正文。",
            "parse_method": "css_extract",
            "attachment_links": [{"url": ATT1, "text": "附件1"}, {"url": ATT2, "text": "附件2"}],
            "extra": {"category": "公告"},
            "source_url": page_data["url"],
            "source_site": site_key,
        }

    def download(pdf_url, parent_uuid="", source_site="", force=False, cookies=None):
        calls["download"].append((pdf_url, cookies))
        if pdf_url == ATT2 and calls.get("fail_second"):
            raise RuntimeError("HTTP 404")
        path = tmp_path / (pdf_url[-6:])
        path.write_bytes(b"\xd0\xcf\x11\xe0 synthetic" + pdf_url.encode())
        return {"uuid": "f", "local_path": str(path), "source_url": pdf_url, "file_ext": "." + pdf_url.rsplit(".", 1)[1],
                "skipped": False}

    monkeypatch.setattr(legacy_sites, "_fetch_page", fetch_page)
    monkeypatch.setattr(legacy_sites, "_route_webpage", route_webpage)
    monkeypatch.setattr(legacy_sites, "_download", download)
    monkeypatch.setattr(legacy_sites, "_store_root", lambda: tmp_path / "store")
    return calls


def records_by_role(batch):
    out = {}
    for r in batch["records"]:
        data = json.loads(r.raw_content)
        out.setdefault(data["role"], []).append((r, data))
    return out


def test_sites_are_connected_through_the_one_framework_but_marked_unverified():
    for source in ("nmpa", "cde", "chictr"):
        spec = catalog.spec(source)
        assert spec.status == "active" and spec.plugin == "legacy_site_crawler"
        assert source in catalog.FETCHABLE and source not in catalog.WATCHABLE
        assert spec.verified is False  # 迁入旧代码，本系统未对原站实测



def test_read_public_dispatches_to_the_migrated_plugin(monkeypatch):
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setattr(legacy_sites, "read_site", lambda request: {"marker": request.source_id})
    assert "legacy_site_crawler" in PLUGINS
    assert read_public(FetchRequest(source_id="chictr", query="by_url", source_record_ids=["https://www.chictr.org.cn/showproj.html?proj=1"])) == {"marker": "chictr"}


def test_detail_page_becomes_capture_plus_one_record_per_attachment(fake_web):
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    assert batch["error"] is None and batch["requests"] == 3
    roles = records_by_role(batch)
    [(page, data)] = roles["web_capture"]
    assert page.source_record_id == PAGE and page.record_kind == "document"
    assert data["schema_version"] == legacy_sites.WEB_SCHEMA and "<h1>" in data["html"]
    assert data["parsed"]["markdown"].startswith("# 关于某药品的公告")
    A1, A2 = legacy_sites.attachment_record_id(PAGE, ATT1), legacy_sites.attachment_record_id(PAGE, ATT2)
    assert [a["record_id"] for a in data["attachments"]] == [A1, A2]
    atts = {r.source_record_id: d for r, d in roles["attachment"]}
    assert set(atts) == {A1, A2}
    first = atts[A1]
    assert first["url"] == ATT1
    assert first["parent_record_id"] == PAGE and len(first["sha256"]) == 64 and first["size"] > 0
    assert first["file_ext"] == ".doc" and first["status"] == "stored"
    # 附件沿用页面会话（旧逻辑：CDE 等站点下载附件要带页面 cookies）
    assert fake_web["download"][0][1] == [{"name": "c"}]


def test_attachment_download_failure_is_a_gap_not_a_failed_page(fake_web):
    fake_web["fail_second"] = True
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    roles = records_by_role(batch)
    [(_, page)] = roles["web_capture"]
    status = {a["url"]: a["status"] for a in page["attachments"]}
    assert status == {ATT1: "stored", ATT2: "download_failed"}
    assert [d["url"] for _, d in roles["attachment"]] == [ATT1]
    assert batch["error"] is None


def test_url_outside_the_site_is_refused(fake_web):
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=["https://evil.example/x"]))
    assert batch["error"]["code"] == "url_outside_site" and batch["records"] == []


def test_list_mode_reuses_old_pagination_and_resumes(fake_web, monkeypatch):
    urls = [f"https://www.nmpa.gov.cn/xxgk/ggtg/{i:04d}.html" for i in range(5)]

    class Driver:
        def iter_detail_urls(self):
            yield from urls

    start = legacy_sites.SITE_PROFILES["nmpa"]["start_urls"][0]
    monkeypatch.setattr(legacy_sites, "_make_driver", lambda site, profile, url: Driver())
    first = read_public(FetchRequest(source_id="nmpa", query=start, max_records=2))
    assert [r.source_record_id for r in first["records"] if json.loads(r.raw_content)["role"] == "web_capture"] == urls[:2]
    cont = first["next_continuation"]
    assert cont["read_count"] == 2 and not first["upstream_pagination_exhausted"]
    second = read_public(
        FetchRequest(source_id="nmpa", query=start, max_records=10, continuation=cont)
    )
    pages = [r.source_record_id for r in second["records"] if json.loads(r.raw_content)["role"] == "web_capture"]
    assert pages == urls[2:] and second["upstream_pagination_exhausted"] and second["next_continuation"] is None


def test_list_mode_only_accepts_the_sites_declared_start_urls(fake_web):
    batch = read_public(FetchRequest(source_id="nmpa", query="https://www.nmpa.gov.cn/other/index.html"))
    assert batch["error"]["code"] == "unknown_start_url"


def test_legacy_attachments_mode_refetches_originals_listed_by_the_old_store(fake_web, monkeypatch):
    monkeypatch.setattr(
        legacy_sites, "_legacy_attachment_links", lambda site, parent: [{"url": ATT1, "text": "附件1"}]
    )
    batch = read_public(FetchRequest(source_id="nmpa", query="legacy_attachments", source_record_ids=[PAGE]))
    roles = records_by_role(batch)
    assert "web_capture" not in roles  # 原页面不重抓
    [(record, data)] = roles["attachment"]
    assert record.source_record_id == legacy_sites.attachment_record_id(PAGE, ATT1) and data["parent_record_id"] == PAGE
    assert data["discovered_from"] == "legacy_attachments_table"
    assert fake_web["fetch"] == []


def test_projection_of_capture_and_attachment(fake_web):
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    page, att = batch["records"][0], batch["records"][1]
    p = project_record("nmpa", page)
    assert p["title"] == "关于某药品的公告" and p["clean_text"].endswith("正文。")
    assert p["source_url"] == PAGE and p["facts"]["role"] == "web_capture"
    assert p["facts"]["attachment_record_ids"] == [
        legacy_sites.attachment_record_id(PAGE, ATT1), legacy_sites.attachment_record_id(PAGE, ATT2)
    ]
    assert p["source_published_at"]["value"] == "2026-11-01"
    a = project_record("nmpa", att)
    assert a["facts"]["role"] == "attachment" and a["facts"]["parent_record_id"] == PAGE
    assert a["source_url"] == ATT1
    assert a["clean_text"] == "" and a["facts"]["sha256"]  # 正文由加工层 attachment_parse 产出
    with pytest.raises(HistoryError):
        project_record(
            "nmpa", ArchiveRecord(source_record_id=PAGE, record_kind="document", raw_content=json.dumps({"x": 1}))
        )


def test_same_file_under_two_channel_pages_is_two_stable_records(fake_web, monkeypatch):
    """NMPA 同一公告在两个栏目各有一个页面地址、附件链接相同（11-04 真实数据）。父页面+附件成对作为记录，
    各自稳定，不会因为从另一个父页面读到而来回换版本。"""
    other = "https://www.nmpa.gov.cn/yaopin/ypggtg/20261101120000123.html"
    monkeypatch.setattr(legacy_sites, "_legacy_attachment_links", lambda site, parent: [{"url": ATT1, "text": "附件1"}])
    batch = read_public(FetchRequest(source_id="nmpa", query="legacy_attachments", source_record_ids=[PAGE, other]))
    ids = [r.source_record_id for r in batch["records"]]
    assert len(set(ids)) == 2 and all(i.endswith("#attachment=" + ATT1) for i in ids)
    again = read_public(FetchRequest(source_id="nmpa", query="legacy_attachments", source_record_ids=[PAGE]))
    assert again["records"][0].raw_content == batch["records"][0].raw_content


def test_very_long_pair_falls_back_to_a_hashed_attachment_key():
    long_url = "https://www.nmpa.gov.cn/" + "x" * 390 + ".pdf"
    rid = legacy_sites.attachment_record_id(PAGE, long_url)
    assert len(rid) <= 400 and rid.startswith(PAGE + "#attachment-sha256=")
