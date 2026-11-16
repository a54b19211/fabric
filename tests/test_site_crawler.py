"""站点插件 fetch.site_crawler（NMPA / CDE / ChiCTR 页面、接口站点、表格页）。

不联网：页面抓取、解析和附件下载都换成替身；旧代码的站点配置和调用顺序原样复用。
"""

import json

import pytest

from fetch import catalog, rate, site_crawler, transport
from fetch.projection import project_record
from fetch.sources import read_public
from store.record_models import ArchiveRecord, FabricError, FetchRequest

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
        transport.wait(url)  # 和真的 fetch_page 一样，打开页面之前等限速、记一个请求
        calls["fetch"].append((url, kwargs))
        return {"page": FakePage(), "url": url, "uuid": "u-" + url[-8:], "timestamp": "t", "cookies": [{"name": "c"}]}

    def route_webpage(page_data, site_key=None):
        return {
            "uuid": page_data["uuid"],
            "title": "关于某药品的公告",
            "publish_date": "2026-11-01",
            "markdown": "# 关于某药品的公告\n\n正文。",
            "parse_method": "css_extract",
            "attachment_links": [{"url": ATT1, "name": "附件1"}, {"url": ATT2, "name": "附件2"}],  # 提取器写 name
            "extra": {"category": "公告"},
            "source_url": page_data["url"],
            "source_site": site_key,
        }

    def download(pdf_url, parent_uuid="", source_site="", cookies=None, store_as="", proxy=None):
        transport.wait(pdf_url)
        calls["download"].append((pdf_url, cookies))
        calls.setdefault("store_as", []).append(store_as)
        if pdf_url == ATT2 and calls.get("fail_second"):
            raise RuntimeError("HTTP 404")
        path = tmp_path / (store_as or pdf_url)[-6:]  # 落盘路径也只看 store_as（有的话），和 store_original 一样
        path.write_bytes(b"\xd0\xcf\x11\xe0 synthetic" + (store_as or pdf_url).encode())  # 同一个文件，每次同样的字节
        ext = "xlsx" if "?" in pdf_url else pdf_url.rsplit(".", 1)[1]
        return {"uuid": "f", "local_path": str(path), "source_url": pdf_url, "file_ext": "." + ext, "skipped": False,
                "file_name": calls.get("file_name", "")}

    monkeypatch.setattr(site_crawler, "fetch_page", fetch_page)
    monkeypatch.setattr(site_crawler, "route_webpage", route_webpage)
    monkeypatch.setattr(site_crawler, "download_pdf", download)
    monkeypatch.setattr(site_crawler, "_store_root", lambda: tmp_path / "store")
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
        assert spec.status == "active" and spec.plugin == "site_crawler"
        assert source in catalog.FETCHABLE and source in catalog.WATCHABLE  # 列表从新到旧：遇到已存的就停
        assert spec.incremental.kind == "stop_at_known"
        assert spec.verified is False  # 迁入旧代码，本系统未对原站实测
    for source in ("hkex", "nccn"):  # 分组读：一家公司、一份指南一组，一个周期读完全部的组
        assert source in catalog.WATCHABLE and catalog.spec(source).backfill
        assert catalog.spec(source).incremental.kind == "stop_at_known" and catalog.spec(source).incremental.by_group
        assert site_crawler.SITE_PROFILES[source]["by_group"]  # 站点配置的拉取函数也是按组给的


def test_read_public_dispatches_to_the_migrated_plugin(monkeypatch):
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setattr(site_crawler, "read_site", lambda request: {"marker": request.source_id})
    batch = read_public(FetchRequest(source_id="chictr", query="by_url",
                                     source_record_ids=["https://www.chictr.org.cn/showproj.html?proj=1"]))
    assert batch["marker"] == "chictr" and batch["requests"] == 0  # 请求数是 transport 这次读取数到的


def test_detail_page_becomes_capture_plus_one_record_per_attachment(fake_web):
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    assert batch["error"] is None and batch["requests"] == 3
    roles = records_by_role(batch)
    [(page, data)] = roles["web_capture"]
    assert page.source_record_id == PAGE and page.record_kind == "document"
    assert data["schema_version"] == site_crawler.WEB_SCHEMA and "<h1>" in data["html"]
    assert data["parsed"]["markdown"].startswith("# 关于某药品的公告")
    A1, A2 = site_crawler.attachment_record_id(PAGE, ATT1), site_crawler.attachment_record_id(PAGE, ATT2)
    assert [a["record_id"] for a in data["attachments"]] == [A1, A2]
    atts = {r.source_record_id: d for r, d in roles["attachment"]}
    assert set(atts) == {A1, A2}
    first = atts[A1]
    assert first["url"] == ATT1
    assert first["parent_record_id"] == PAGE and len(first["sha256"]) == 64 and first["size"] > 0
    assert first["file_ext"] == ".doc" and first["status"] == "stored"
    assert first["file_name"] == "附件1" and atts[A2]["file_name"] == "附件2"  # 抽取器给的 name，不是网址末段
    assert [a["text"] for a in data["attachments"]] == ["附件1", "附件2"]
    # 附件沿用页面会话（旧逻辑：CDE 等站点下载附件要带页面 cookies）
    assert fake_web["download"][0][1] == [{"name": "c"}]


def test_links_read_the_label_from_text_or_name():
    parsed = {"attachment_links": [
        {"url": "u1", "name": "从 name 读"}, {"url": "u2", "text": "从 text 读"}, {"url": "u3", "text": "text", "name": "name"},
        {"url": "u4"}, {"text": "没有网址"}, None, {"url": "u5", "key": "字段名"}]}
    assert site_crawler._links(parsed) == [
        {"url": "u1", "text": "从 name 读"}, {"url": "u2", "text": "从 text 读"}, {"url": "u3", "text": "text"},
        {"url": "u4", "text": ""}, {"url": "u5", "text": "", "key": "字段名"}]
    assert site_crawler._links({}) == [] and site_crawler._links({"attachment_links": None}) == []


CHICTR = "https://www.chictr.org.cn/showproj.html?proj=317987"
CHICTR_ATT = CHICTR + "#attachment=统计结果"


def _chictr_route(monkeypatch, tokens):
    """ChiCTR 页面替身：每次读到的下载链接带一个不同的令牌，附件有固定的 key（页面上的字段名）。"""
    tokens = iter(tokens)

    def route_webpage(page_data, site_key=None):
        link = {"url": "https://www.chictr.org.cn/bin/chictr/DownloadTest?path=" + next(tokens), "key": "统计结果"}
        return {"uuid": "u", "title": "某试验", "publish_date": "2026-11-11", "markdown": "# 某试验\n\n正文。",
                "parse_method": "css_extract", "attachment_links": [link], "source_url": page_data["url"],
                "source_site": site_key}

    monkeypatch.setattr(site_crawler, "route_webpage", route_webpage)


def test_attachment_whose_link_changes_on_every_visit_is_named_by_its_key_and_the_link_is_not_kept(fake_web, monkeypatch):
    """ChiCTR 的下载链接带一次性令牌（每次打开页面都不同）。附件身份是“父记录#attachment=字段名”，令牌只在下载那一刻用，
    不进任何原件：同一页面连读几遍，页面和附件的原件逐字相同（不新增版本）；文件名用站点在响应头里给的。"""
    fake_web["file_name"] = "4C3F5162.xlsx"
    _chictr_route(monkeypatch, ["TOKEN1", "TOKEN2", "TOKEN3"])
    rounds = [read_public(FetchRequest(source_id="chictr", query="by_url", source_record_ids=[CHICTR]))
              for _ in range(3)]
    for batch in rounds:
        assert batch["error"] is None and [r.source_record_id for r in batch["records"]] == [CHICTR, CHICTR_ATT]
        assert [a["status"] for a in batch["attachment_coverage"]] == ["stored"]
        assert "TOKEN" not in json.dumps(batch["attachment_coverage"])
        assert all("TOKEN" not in r.raw_content for r in batch["records"])
    assert [url[-6:] for url, _ in fake_web["download"]] == ["TOKEN1", "TOKEN2", "TOKEN3"]  # 令牌只用在下载那一刻
    assert fake_web["store_as"] == [CHICTR_ATT] * 3  # 原件每次落在同一处
    for later in rounds[1:]:
        assert [r.raw_content for r in later["records"]] == [r.raw_content for r in rounds[0]["records"]]
    page, att = (json.loads(r.raw_content) for r in rounds[0]["records"])
    assert page["attachments"] == [{"record_id": CHICTR_ATT, "url": "", "text": ""}]
    assert att["key"] == "统计结果" and att["url"] == "" and att["file_name"] == "4C3F5162.xlsx"
    projected = project_record("chictr", rounds[0]["records"][1])  # 身份核对用 key，不是网址
    assert projected["facts"]["parent_record_id"] == CHICTR and projected["source_url"] is None
    assert projected["title"] == "4C3F5162.xlsx"


def test_keyed_attachment_without_a_file_name_in_the_response_is_named_by_its_key(fake_web, monkeypatch):
    _chictr_route(monkeypatch, ["T"])
    batch = read_public(FetchRequest(source_id="chictr", query="by_url", source_record_ids=[CHICTR]))
    assert json.loads(batch["records"][1].raw_content)["file_name"] == "统计结果"


def test_attachment_download_failure_is_a_gap_not_a_failed_page(fake_web):
    fake_web["fail_second"] = True
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    roles = records_by_role(batch)
    status = {a["url"]: a["status"] for a in batch["attachment_coverage"]}
    assert status == {ATT1: "stored", ATT2: "download_failed"}
    assert [d["url"] for _, d in roles["attachment"]] == [ATT1]
    assert batch["error"] is None


def test_url_outside_the_site_is_refused(fake_web):
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=["https://evil.example/x"]))
    assert batch["error"]["code"] == "url_outside_site" and batch["records"] == []


def pages_of(batch):
    return [r.source_record_id for r in batch["records"] if json.loads(r.raw_content)["role"] == "web_capture"]


def test_list_mode_reuses_old_pagination_and_resumes(fake_web, monkeypatch):
    """max_records 数的是版本（父页面和附件都算）：每页 1 个页面 + 2 个附件。"""
    urls = [f"https://www.nmpa.gov.cn/xxgk/ggtg/{i:04d}.html" for i in range(5)]

    class Driver:
        def iter_detail_urls(self):
            yield from urls

    start = site_crawler.SITE_PROFILES["nmpa"]["start_urls"][0]
    monkeypatch.setattr(site_crawler, "make_driver", lambda site, profile, url: Driver())
    first = read_public(FetchRequest(source_id="nmpa", query=start, max_records=5))
    assert pages_of(first) == urls[:2] and len(first["records"]) == 5
    assert first["stop_reason"] == "record_budget" and not first["upstream_pagination_exhausted"]
    cont = first["next_continuation"]
    assert json.loads(cont["cursor"]) == {"i": 1, "a": 1, "p": 1}  # 第 2 页已保存，第 2 个附件还没读
    second = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100, continuation=cont))
    # 第 2 页不再保存一次，只读它剩下的附件，然后接着读后面的页
    assert pages_of(second) == urls[2:] and second["upstream_pagination_exhausted"]
    assert second["next_continuation"] is None
    assert second["attachment_coverage"][0]["url"] == ATT2 and second["attachment_coverage"][0]["status"] == "stored"


def test_full_reader_stops_asking_the_driver_for_more_list_pages(fake_web, monkeypatch):
    """翻一页交一页：读满 max_records 就不再要下一页，并让驱动收工（浏览器随之关闭）。"""
    seen = {"pages": 0, "closed": False}

    class Driver:
        def iter_detail_urls(self):
            try:
                for page in range(200):  # 站点配置翻 200 页
                    seen["pages"] += 1
                    yield f"https://www.nmpa.gov.cn/xxgk/ggtg/{page:04d}.html"
            finally:
                seen["closed"] = True

    start = site_crawler.SITE_PROFILES["nmpa"]["start_urls"][0]
    monkeypatch.setattr(site_crawler, "make_driver", lambda site, profile, url: Driver())
    batch = read_public(FetchRequest(source_id="nmpa", query=start, max_records=3))
    assert batch["stop_reason"] == "record_budget" and len(batch["records"]) == 3
    assert seen == {"pages": 2, "closed": True}  # 第 1 页 + 它的 2 个附件，第 2 页占满名额；第 3 页没有去要


def test_codex_r59_record_budget_is_enforced_for_by_url(fake_web):
    """r59 探针：max_records=1、三个合法站内 URL，以前返回 9 个版本并声称读到底。"""
    urls = [PAGE, PAGE.replace("123.html", "124.html"), PAGE.replace("123.html", "125.html")]
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=urls, max_records=1))
    assert len(batch["records"]) == 1 and batch["stop_reason"] == "record_budget"
    assert not batch["upstream_pagination_exhausted"]
    assert [a["status"] for a in batch["attachment_coverage"]] == ["not_attempted", "not_attempted"]
    assert json.loads(batch["next_continuation"]["cursor"]) == {"i": 0, "a": 0, "p": 1}


def test_codex_r61_resuming_with_the_smallest_budget_still_makes_progress(fake_web):
    """max_records=1 按返回的续读位置一直读下去：每轮都要前进，最后三页各自的页面和附件都读到。"""
    urls = [PAGE, PAGE.replace("123.html", "124.html"), PAGE.replace("123.html", "125.html")]
    cont, saved, rounds = None, [], 0
    while True:
        rounds += 1
        batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=urls, max_records=1,
                                         continuation=cont))
        assert len(batch["records"]) == 1  # 每轮都保存了一个新版本，没有原地打转
        saved += [r.source_record_id for r in batch["records"]]
        cont = batch["next_continuation"]
        if cont is None:
            break
        assert rounds < 20
    assert batch["upstream_pagination_exhausted"] and rounds == 9 and len(set(saved)) == 9


def test_every_request_is_counted_and_none_is_limited_by_the_page_budget(fake_web):
    urls = [PAGE, PAGE.replace("123.html", "124.html")]
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=urls, max_pages=1,
                                     max_records=100))
    # 两页各 1 个页面请求 + 2 个附件 = 6；请求次数只计数，不设额度（max_pages 不折成请求数上限）
    assert batch["requests"] == 6 and batch["upstream_pagination_exhausted"]


def test_too_large_attachment_is_a_recorded_gap(fake_web, monkeypatch):
    def too_large(*a, **k):
        raise ValueError("response_too_large")

    monkeypatch.setattr(site_crawler, "download_pdf", too_large)
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    assert {a["status"] for a in batch["attachment_coverage"]} == {"too_large"} and len(batch["records"]) == 1


def test_a_link_that_turns_out_to_be_a_web_page_is_a_recorded_gap_not_an_attachment(fake_web, monkeypatch):
    def a_page(*a, **k):
        raise ValueError("html_page_not_a_file")

    monkeypatch.setattr(site_crawler, "download_pdf", a_page)
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    assert {a["status"] for a in batch["attachment_coverage"]} == {"not_a_file"} and len(batch["records"]) == 1


def test_codex_r61_same_page_read_twice_is_the_same_raw(fake_web):
    """抓取时间、本次附件读没读到都不进原件：同一页面重读不制造新版本。"""
    a = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    fake_web["fail_second"] = True
    b = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    assert a["records"][0].raw_content == b["records"][0].raw_content
    assert "fetched_at" not in json.loads(a["records"][0].raw_content)


def test_list_mode_only_accepts_the_sites_declared_start_urls(fake_web):
    batch = read_public(FetchRequest(source_id="nmpa", query="https://www.nmpa.gov.cn/other/index.html"))
    assert batch["error"]["code"] == "unknown_start_url"


def test_stored_attachments_mode_refetches_originals_listed_in_the_documents_store(fake_web, monkeypatch):
    monkeypatch.setattr(
        site_crawler, "_stored_attachment_links", lambda site, parent: [{"url": ATT1, "text": "附件1"}]
    )
    batch = read_public(FetchRequest(source_id="nmpa", query="stored_attachments", source_record_ids=[PAGE]))
    roles = records_by_role(batch)
    assert "web_capture" not in roles  # 原页面不重抓
    [(record, data)] = roles["attachment"]
    assert record.source_record_id == site_crawler.attachment_record_id(PAGE, ATT1) and data["parent_record_id"] == PAGE
    assert data["discovered_from"] == "stored_attachments_table"
    assert fake_web["fetch"] == []


def test_projection_of_capture_and_attachment(fake_web):
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    page, att = batch["records"][0], batch["records"][1]
    p = project_record("nmpa", page)
    assert p["title"] == "关于某药品的公告" and p["clean_text"].endswith("正文。")
    assert p["source_url"] == PAGE and p["facts"]["role"] == "web_capture"
    assert p["facts"]["attachment_record_ids"] == [
        site_crawler.attachment_record_id(PAGE, ATT1), site_crawler.attachment_record_id(PAGE, ATT2)
    ]
    assert p["source_published_at"]["value"] == "2026-11-01"
    a = project_record("nmpa", att)
    assert a["facts"]["role"] == "attachment" and a["facts"]["parent_record_id"] == PAGE
    assert a["source_url"] == ATT1
    assert a["clean_text"] == "" and a["facts"]["sha256"]  # 正文由加工层 attachment_parse 产出
    with pytest.raises(FabricError):
        project_record(
            "nmpa", ArchiveRecord(source_record_id=PAGE, record_kind="document", raw_content=json.dumps({"x": 1}))
        )


def test_same_file_under_two_channel_pages_is_two_stable_records(fake_web, monkeypatch):
    """NMPA 同一公告在两个栏目各有一个页面地址、附件链接相同（11-04 真实数据）。父页面+附件成对作为记录，
    各自稳定，不会因为从另一个父页面读到而来回换版本。"""
    other = "https://www.nmpa.gov.cn/yaopin/ypggtg/20261101120000123.html"
    monkeypatch.setattr(site_crawler, "_stored_attachment_links", lambda site, parent: [{"url": ATT1, "text": "附件1"}])
    batch = read_public(FetchRequest(source_id="nmpa", query="stored_attachments", source_record_ids=[PAGE, other]))
    ids = [r.source_record_id for r in batch["records"]]
    assert len(set(ids)) == 2 and all(i.endswith("#attachment=" + ATT1) for i in ids)
    again = read_public(FetchRequest(source_id="nmpa", query="stored_attachments", source_record_ids=[PAGE]))
    assert again["records"][0].raw_content == batch["records"][0].raw_content


def test_very_long_pair_falls_back_to_a_hashed_attachment_key():
    long_url = "https://www.nmpa.gov.cn/" + "x" * 390 + ".pdf"
    rid = site_crawler.attachment_record_id(PAGE, long_url)
    assert len(rid) <= 400 and rid.startswith(PAGE + "#attachment-sha256=")


def test_api_site_records_become_site_record_versions_with_their_attachments(fake_web, monkeypatch):
    """接口站点（如 openFDA、港交所）走同一个插件：每条接口记录一个 site_record 版本，原始字段保留，
    记录身份是 uuid5(站点:记录键)；结果里列出的附件各成版本。"""
    profile = {"source_type": "api", "api": {}, "uid_field": "id",
               "field_map": {"title": "t", "publish_date": "d", "source_url": "u"},
               "api_fetcher": lambda cfg: iter([{"id": "R1", "t": "召回一", "d": "2026-01-02", "u": "https://x.invalid/1"},
                                                {"id": "R2", "t": "召回二", "d": "", "u": "https://x.invalid/2"}])}
    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_enforcement", profile)
    batch = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=1))
    [record] = batch["records"]
    data = json.loads(record.raw_content)
    assert data["role"] == "site_record" and data["raw"]["id"] == "R1" and data["parsed"]["title"] == "召回一"
    from store.record_models import make_uuid as _make_uuid
    assert record.source_record_id == _make_uuid("fda_api_enforcement:R1")
    assert batch["stop_reason"] == "record_budget" and batch["next_continuation"]
    projected = site_crawler.project_capture("fda_api_enforcement", record)
    assert projected["title"] == "召回一" and projected["parser_version"] == "datafabric.site.site_record.v1"
    again = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=5,
                                     continuation=batch["next_continuation"]))
    assert [json.loads(r.raw_content)["raw"]["id"] for r in again["records"]] == ["R2"]
    assert again["upstream_pagination_exhausted"]


def test_api_site_only_answers_records_query(fake_web, monkeypatch):
    profile = {"source_type": "api", "api": {}, "api_fetcher": lambda cfg: iter([])}
    monkeypatch.setitem(site_crawler.SITE_PROFILES, "hkex", profile)
    batch = read_public(FetchRequest(source_id="hkex", query="https://www1.hkexnews.hk/x"))
    assert batch["stop_reason"] == "source_failed" and batch["error"]["code"] == "unknown_query"


def test_failed_site_read_keeps_the_real_reason_without_secrets(fake_web, monkeypatch):
    """The reason used to be the class name only ("站点读取失败：TimeoutError"). Now it is the first line of the
    error message; the rest of the browser's call log, URL query strings and key=value pairs stay out."""
    class Driver:
        def iter_detail_urls(self):
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating to "
                               '"https://www.nmpa.gov.cn/x?api_key=SECRET123", waiting until "load"')
            yield

    start = site_crawler.SITE_PROFILES["nmpa"]["start_urls"][0]
    monkeypatch.setattr(site_crawler, "make_driver", lambda site, profile, url: Driver())
    batch = read_public(FetchRequest(source_id="nmpa", query=start))
    assert batch["error"] == {"code": "source_read_failed",
                              "message": "站点读取失败：TimeoutError：Page.goto: Timeout 30000ms exceeded."}
    assert batch["stop_reason"] == "source_failed"


@pytest.mark.parametrize(("raw", "shown"), [
    ("GET https://api.example.org/v1/x?api_key=SECRET123 failed", "GET https://api.example.org/v1/x failed"),
    ("bad request, token=abc123 refused", "bad request, token=*** refused"),
    ("", ""),
])
def test_failure_reason_drops_secrets_and_keeps_the_class_name(raw, shown):
    assert site_crawler._why(RuntimeError(raw)) == "RuntimeError" + ("：" + shown if shown else "")


# ── 读取中出错不能被吞：HTTP 错误和网络异常是失败（写明原因），不是“读到底了” ─────────────────

def _http(status, body=None, headers=None):
    import httpx

    return httpx.Response(status, json=body if body is not None else {}, headers=headers or {},
                          request=httpx.Request("GET", "https://api.example.invalid/x"))


OPENFDA_RATE_LIMIT = {"error": {"code": "OVER_RATE_LIMIT",
                                "message": "Too many requests: the limit for your IP has been reached."}}


@pytest.mark.parametrize("source", ["fda_api_enforcement", "fda_api_drug_label"])  # skip 翻页 / search_after 翻页
@pytest.mark.parametrize(("answer", "reason"), [
    (lambda: _http(429, OPENFDA_RATE_LIMIT), "HTTP 429"),
    (lambda: _http(503, {"error": {"code": "SERVER_ERROR"}}), "HTTP 503"),
    (lambda: (_ for _ in ()).throw(__import__("httpx").ConnectTimeout("timed out")), "ConnectTimeout"),
])
def test_openfda_http_errors_and_timeouts_are_failed_reads_that_say_so(monkeypatch, source, answer, reason):
    """openFDA 回 429 / 503 或连接超时，以前记一行日志就当读到底（运行记为“完成、没有数据”）。"""
    monkeypatch.setattr(transport, "get", lambda *a, **k: answer())
    batch = read_public(FetchRequest(source_id=source, query="records"))
    assert batch["error"]["code"] == "source_read_failed"
    assert reason in batch["error"]["message"] and "已读 0 条" in batch["error"]["message"]
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]
    assert batch["records"] == []


def test_openfda_failure_on_a_later_page_keeps_what_was_read_and_still_fails(monkeypatch):
    """第二页出错：第一页读到的原件保留，但这次读取不是“读完了”。"""
    record = {"set_id": "S1", "effective_time": "20261101", "openfda": {},
              "spl_product_data_elements": ["Some Product SOME INGREDIENT"]}
    link = '<https://api.fda.gov/drug/label.json?search_after=1&sort=effective_time:desc>; rel="next"'
    answers = iter([_http(200, {"meta": {"results": {"total": 3}}, "results": [record]}, {"Link": link}),
                    _http(500, {"error": {"code": "SERVER_ERROR"}})])
    monkeypatch.setattr(transport, "get", lambda *a, **k: next(answers))
    batch = read_public(FetchRequest(source_id="fda_api_drug_label", query="records", max_records=5))
    assert len(batch["records"]) == 1
    assert "HTTP 500" in batch["error"]["message"] and "已读 1 条" in batch["error"]["message"]
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]


def test_openfda_404_past_the_last_skip_page_is_still_the_normal_end(monkeypatch):
    """skip 翻页翻过最后一页，openFDA 回 404：这是读完了，不是失败。"""
    # 召回站点的配置现在指定了 sort（新的在前，走 search_after）；skip 翻页是没指定 sort 时的读法，这里去掉 sort 测它
    monkeypatch.delitem(site_crawler.SITE_PROFILES["fda_api_enforcement"]["api"], "sort")
    record = {"recall_number": "D-1-2026", "product_description": "Some product", "report_date": "20261101"}
    answers = iter([_http(200, {"meta": {"results": {"total": 5}}, "results": [record]}),
                    _http(404, {"error": {"code": "NOT_FOUND", "message": "No matches found!"}})])
    monkeypatch.setattr(transport, "get", lambda *a, **k: next(answers))
    batch = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=10))
    assert len(batch["records"]) == 1 and batch["error"] is None
    assert batch["upstream_pagination_exhausted"] and batch["stop_reason"] == "upstream_exhausted"


class _RochePage:
    """Roche 列表页替身：只有状态码，页面上没有新闻条目。"""
    def __init__(self, status):
        self.status = status

    def css(self, selector):
        return []

    def find_all(self, matcher):
        return []


def _roche_one_year(monkeypatch, fetch):
    from fetch.extractors.roche import roche_web_fetcher

    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    for name in ("year_start", "year_end"):
        monkeypatch.setitem(site_crawler.SITE_PROFILES["roche"]["api"], name, 2026)  # 只读 2026 年
    monkeypatch.setattr(roche_web_fetcher.StealthyFetcher, "fetch", staticmethod(fetch))
    return read_public(FetchRequest(source_id="roche", query="records"))


def test_roche_starts_at_this_year_when_the_start_year_is_zero_or_not_given(monkeypatch):
    """起始年份以前写死 2026：到了 2027 年，新年的新闻稿读不到。0 或不写 = 今年。"""
    from datetime import datetime, timezone

    from fetch.extractors.roche import roche_web_fetcher as fetcher

    seen = []
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)

    def fetch(url, **kwargs):
        seen.append(url)
        raise RuntimeError("读第一页就够了")

    monkeypatch.setattr(fetcher.StealthyFetcher, "fetch", staticmethod(fetch))
    for cfg in ({}, {"year_start": 0}, site_crawler.SITE_PROFILES["roche"]["api"]):
        seen.clear()
        with pytest.raises(RuntimeError):
            next(fetcher.iter_roche_web(cfg))
        assert seen[0].endswith(f"#/year/{datetime.now(timezone.utc).year}/page/1")


def test_roche_list_page_timeout_is_a_failed_read_with_the_real_reason(monkeypatch):
    def fetch(url, **kwargs):
        raise TimeoutError("Page.goto: Timeout 60000ms exceeded.")

    batch = _roche_one_year(monkeypatch, fetch)
    assert batch["error"] == {"code": "source_read_failed",
                              "message": "站点读取失败：TimeoutError：Page.goto: Timeout 60000ms exceeded."}
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]


def test_roche_list_page_http_error_is_a_failed_read_but_an_empty_page_still_ends_the_year(monkeypatch):
    batch = _roche_one_year(monkeypatch, lambda url, **kw: _RochePage(503))
    assert batch["error"] == {"code": "source_read_failed",
                              "message": "Roche 列表页读取失败：2026 年第 1 页返回 HTTP 503（不当作读到底）"}
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]
    # 页面读到了但一条新闻也没解析出来：不是“没有新内容”，是可能改版——失败，不当正常读完（这里只给了一年；
    # 真实读取从 2026 年一路往前，某一年没有（更多）新闻只是跳到下一年）
    empty = _roche_one_year(monkeypatch, lambda url, **kw: _RochePage(200))
    assert empty["error"] == {"code": "list_empty", "message": site_crawler.LIST_EMPTY}
    assert empty["stop_reason"] == "source_failed" and not empty["upstream_pagination_exhausted"]


class _FierceNode:
    attrib = {}

    def __init__(self, text):
        self.text, self.html_content = text, "<p>" + text + "</p>"

    def get_all_text(self, strip=False):
        return self.text


class _FiercePage:
    """Fierce Pharma 详情页替身：状态码、标题（h1.article-title）和正文（article）。"""
    def __init__(self, status, title="", body=""):
        self.status = status
        self._parts = {"h1.article-title": title, "article": body}

    def css(self, selector):
        return [_FierceNode(self._parts[selector])] if self._parts.get(selector) else []


WHOLE = _FiercePage(200, "Merck KGaA lands FDA nod", "正文。" * 400)


def _fierce_read(monkeypatch, pages, max_records=5, retry_delay=0):
    """pages：文章网址 → 依次给出的答复（页面替身，或要抛出的异常）。RSS 里的标题是空的，和真实情况一致。"""
    from fetch.extractors.fiercepharma import fiercepharma_fetcher as fetcher

    monkeypatch.setattr(fetcher, "RETRY_DELAY", retry_delay)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setattr(fetcher, "_fetch_rss_items", lambda cfg: ([
        {"url": url, "guid": "", "title": "", "description": "RSS 摘要", "pub_date": "2026-11-01"} for url in pages], []))

    def fetch(url, **kwargs):
        answer = pages[url].pop(0)
        if callable(answer):  # 要在读取中间做点什么，再给答复
            answer = answer()
        if isinstance(answer, BaseException):  # transport.Stop 是 BaseException
            raise answer
        return answer

    monkeypatch.setattr(fetcher.StealthyFetcher, "fetch", staticmethod(fetch))
    return read_public(FetchRequest(source_id="fiercepharma", query="records", max_records=max_records))


def test_fiercepharma_article_blocked_once_is_read_again_and_stored_whole(monkeypatch):
    a, b = "https://www.fiercepharma.com/pharma/a", "https://www.fiercepharma.com/pharma/b"
    batch = _fierce_read(monkeypatch, {a: [_FiercePage(403), WHOLE], b: [WHOLE]})
    assert batch["error"] is None and batch["upstream_pagination_exhausted"]
    stored = [json.loads(r.raw_content)["parsed"] for r in batch["records"]]
    assert [p["title"] for p in stored] == ["Merck KGaA lands FDA nod"] * 2
    assert all(len(p["markdown"]) > 1000 and "RSS 摘要" not in p["markdown"] for p in stored)


@pytest.mark.parametrize("answers, reason", [
    ([_FiercePage(403), _FiercePage(403)], "HTTP 403"),
    ([_FiercePage(200, "", "短"), _FiercePage(200, "", "短")], "页面没读全（标题 0 字，正文 1 字）"),
    ([TimeoutError("Page.goto: Timeout 60000ms exceeded."), _FiercePage(502)], "HTTP 502"),
    ([_FiercePage(200), TimeoutError("Page.goto: Timeout 60000ms exceeded.")],
     "TimeoutError：Page.goto: Timeout 60000ms exceeded."),
])
def test_fiercepharma_article_that_stays_unread_is_noted_and_the_read_goes_on(monkeypatch, answers, reason):
    """读了两次都不行的一篇：不存版本，网址和原因记进 unread_items，后面的文章照读；读到底时是“有 1 条读不到”。"""
    a, b, c = (f"https://www.fiercepharma.com/pharma/{x}" for x in "abc")
    batch = _fierce_read(monkeypatch, {a: [WHOLE], b: answers, c: [WHOLE]})
    why = f"FiercePharma 详情页读取失败（已重读一次）：{reason}"
    assert batch["unread_items"] == [{"url": b, "reason": why}]
    assert batch["error"] == {"code": "items_unread", "message": f"有 1 条读不到，如 {b}：{why}"}
    assert batch["stop_reason"] == "items_unread" and not batch["upstream_pagination_exhausted"]
    assert [json.loads(r.raw_content)["url"] for r in batch["records"]] == [a, c]  # 坏的这篇没存，后面的存了


def test_fiercepharma_when_no_article_can_be_read_nothing_is_stored_and_every_one_is_listed(monkeypatch):
    a, b = (f"https://www.fiercepharma.com/pharma/{x}" for x in "ab")
    batch = _fierce_read(monkeypatch, {a: [_FiercePage(403)] * 2, b: [_FiercePage(403)] * 2})
    assert batch["records"] == [] and [u["url"] for u in batch["unread_items"]] == [a, b]
    assert batch["error"]["code"] == "items_unread" and batch["error"]["message"].startswith("有 2 条读不到")


def test_fiercepharma_unread_articles_do_not_use_up_the_record_budget(monkeypatch):
    """max_records 数的是版本：读不到的不占名额，读满之后照常停在 record_budget，读不到的清单还在。"""
    a, b, c, d = (f"https://www.fiercepharma.com/pharma/{x}" for x in "abcd")
    batch = _fierce_read(monkeypatch, {a: [_FiercePage(403)] * 2, b: [WHOLE], c: [WHOLE], d: [WHOLE]}, max_records=2)
    assert [json.loads(r.raw_content)["url"] for r in batch["records"]] == [b, c]
    assert batch["stop_reason"] == "record_budget" and batch["error"] is None
    assert [u["url"] for u in batch["unread_items"]] == [a]
    assert json.loads(batch["next_continuation"]["cursor"])["i"] == 3  # 续读从第 4 篇接


def test_fiercepharma_deadline_during_a_reread_is_a_stop_and_keeps_what_was_stored(monkeypatch):
    """截止时间到的时候单篇正在读：运行是“到期”（停在这一篇、给续读位置），不是失败；已存的保留，这一篇不算读不到。"""
    a, b, c = (f"https://www.fiercepharma.com/pharma/{x}" for x in "abc")
    expired = lambda: transport.Stop("deadline")  # noqa: E731 -- 到点了，读取停下
    batch = _fierce_read(monkeypatch, {a: [WHOLE], b: [_FiercePage(403), expired], c: [WHOLE]})
    assert batch["stop_reason"] == "deadline" and batch["error"] is None and batch["unread_items"] == []
    assert [json.loads(r.raw_content)["url"] for r in batch["records"]] == [a]
    assert json.loads(batch["next_continuation"]["cursor"])["i"] == 1


def test_fiercepharma_a_feed_that_cannot_be_read_is_noted_and_the_other_feed_is_still_read(monkeypatch):
    """一个 RSS 读不到（以前只记日志，当成“这个 feed 没有文章”）：记成读不到的条目，别的 feed 照读，运行是“有 1 条读不到”。"""
    from fetch.extractors.fiercepharma import fiercepharma_fetcher as fetcher

    good, bad = "https://www.fiercepharma.com/rss/Pharma/xml", "https://www.fiercepharma.com/rss/manufacturing/xml"
    rss = (b"<rss><channel><item><link>https://www.fiercepharma.com/pharma/a</link>"
           b"<pubDate>Nov 1, 2026 1:55pm</pubDate></item></channel></rss>")

    class Reply:
        def __init__(self, url):
            self.content = rss if url == good else b"<html>blocked</html>"
            self.status = 200 if url == good else 403

        def raise_for_status(self):
            if self.status != 200:
                raise RuntimeError(f"HTTP {self.status}")

    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setattr(transport, "get", lambda url, **kwargs: Reply(url))
    monkeypatch.setattr(fetcher.StealthyFetcher, "fetch", staticmethod(lambda url, **kwargs: WHOLE))
    monkeypatch.setitem(site_crawler.SITE_PROFILES["fiercepharma"]["api"], "rss_feeds", [good, bad])
    batch = read_public(FetchRequest(source_id="fiercepharma", query="records", max_records=5))
    assert [u["url"] for u in batch["unread_items"]] == [bad] and "HTTP 403" in batch["unread_items"][0]["reason"]
    assert [json.loads(r.raw_content)["url"] for r in batch["records"]] == ["https://www.fiercepharma.com/pharma/a"]
    assert batch["error"]["code"] == "items_unread"


def test_nccn_a_detail_page_that_cannot_be_read_is_noted_and_no_list_only_version_is_stored(monkeypatch):
    """以前存一个只有列表页信息的降级版本（没有 PDF 链接）；之后读到全的，它就成了“被改过”的旧版本。"""
    import scrapling.fetchers

    from fetch.extractors.nccn import nccn_fetcher

    items = [{"title": f"Guideline {i}", "version": "1.2026", "detail_url": f"https://www.nccn.org/g/{i}",
              "guideline_id": str(i), "category_name": "Supportive Care"} for i in range(2)]

    class Fetcher:
        def __init__(self, **kwargs):
            pass

        def fetch(self, url, **kwargs):
            if url.endswith("/0"):
                raise TimeoutError("Page.goto: Timeout 45000ms exceeded.")
            return "detail page"

    monkeypatch.setattr(scrapling.fetchers, "StealthyFetcher", Fetcher)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setattr(nccn_fetcher, "_parse_rendered_list", lambda page, cat_num, cat_name: items)
    pdf = {"label": "Guidelines", "url": "https://www.nccn.org/pdf/g1.pdf", "version": "2.2026"}
    monkeypatch.setattr(nccn_fetcher, "extract_nccn_detail", lambda page: {
        "title": "", "version": "2.2026", "attachment_links": [pdf],
        "sections": [{"section_name": "Guidelines", "pdfs": [pdf]}]})
    monkeypatch.setitem(site_crawler.SITE_PROFILES["nccn"]["api"], "categories", ["Supportive Care"])

    def no_download(*args, **kwargs):  # 附件下载不是这个测试要查的：不连外网，只当这个附件没下载成功（记缺口，不存版本）
        raise ValueError("no network in unit tests")

    monkeypatch.setattr(site_crawler, "download_pdf", no_download)
    batch = read_public(FetchRequest(source_id="nccn", query="records", max_records=5))
    assert [u["url"] for u in batch["unread_items"]] == ["https://www.nccn.org/g/0"]
    assert len(batch["records"]) == 1 and batch["error"]["code"] == "items_unread"  # 只有读到了详情页的那一篇存了
    stored = json.loads(batch["records"][0].raw_content)["parsed"]  # 详情页没给标题就用列表页的；版本、发布年份、PDF 用详情页的
    assert stored["title"] == "Guideline 1" and stored["publish_date"] == "2026" and stored["parse_method"] == "nccn_css_extract"
    assert "[Guidelines](https://www.nccn.org/pdf/g1.pdf) (Version 2.2026)" in stored["markdown"]
    assert stored["structured_data"]["pdf_url"] == "https://www.nccn.org/pdf/g1.pdf"


def test_nccn_a_category_list_that_cannot_be_rendered_fails_the_read(monkeypatch):
    """以前只记日志、跳过这个分类：整次读取照样“读到底”，少了一整个分类。"""
    import scrapling.fetchers

    class Fetcher:
        def __init__(self, **kwargs):
            pass

        def fetch(self, url, **kwargs):
            raise TimeoutError("Page.goto: Timeout 45000ms exceeded.")

    monkeypatch.setattr(scrapling.fetchers, "StealthyFetcher", Fetcher)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    batch = read_public(FetchRequest(source_id="nccn", query="records", max_records=5))
    assert batch["records"] == [] and batch["stop_reason"] == "source_failed"
    assert batch["error"]["code"] == "source_read_failed" and "TimeoutError" in batch["error"]["message"]


class _RocheDetail:
    """Roche 详情页替身：状态码和正文（article 里的文字）。"""
    def __init__(self, status, body=""):
        self.status, self.body = status, body

    def css(self, selector):
        return [self] if self.body and selector == "article" else []

    def get_all_text(self, **kwargs):
        return self.body


ROCHE_WHOLE = _RocheDetail(200, "Roche today announced new data. " * 20)


def _roche_read(monkeypatch, details):
    """details：文章网址 → 依次给出的答复（详情页替身，或要抛出的异常）。列表页：一页，列出这些文章，下一页是空的。"""
    from fetch.extractors.roche import roche_web_fetcher as fetcher

    api = site_crawler.SITE_PROFILES["roche"]["api"]
    for name, value in {"year_start": 2026, "year_end": 2026}.items():
        monkeypatch.setitem(api, name, value)
    monkeypatch.setattr(fetcher, "RETRY_DELAY", 0)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    pages = iter([[{"title": "Title " + url[-1], "source_url": url, "publish_date": "2026-11-10"} for url in details], []])
    monkeypatch.setattr(fetcher, "_parse_list_page", lambda page, collected, year, number: next(pages))

    def fetch(url, **kwargs):
        if "#/year/" in url:
            return _RochePage(200)
        answer = details[url].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(fetcher.StealthyFetcher, "fetch", staticmethod(fetch))
    return read_public(FetchRequest(source_id="roche", query="records", max_records=5))


def test_roche_detail_page_read_again_after_a_failure_is_stored_whole(monkeypatch):
    a, b = (f"https://www.roche.com/media/releases/{x}" for x in "ab")
    batch = _roche_read(monkeypatch, {a: [_RocheDetail(503), ROCHE_WHOLE], b: [TimeoutError("x"), ROCHE_WHOLE]})
    assert batch["error"] is None and batch["upstream_pagination_exhausted"] and batch["unread_items"] == []
    assert [json.loads(r.raw_content)["url"] for r in batch["records"]] == [a, b]
    assert all("Roche today announced" in json.loads(r.raw_content)["parsed"]["markdown"] for r in batch["records"])


@pytest.mark.parametrize("answers, reason", [
    ([_RocheDetail(403), _RocheDetail(403)], "HTTP 403"),
    ([TimeoutError("Page.goto: Timeout 60000ms exceeded."), _RocheDetail(502)], "HTTP 502"),
    ([_RocheDetail(200, ""), _RocheDetail(200, "")], "正文为空"),
    ([_RocheDetail(200, ""), TimeoutError("Page.goto: Timeout 60000ms exceeded.")],
     "TimeoutError：Page.goto: Timeout 60000ms exceeded."),
])
def test_roche_detail_page_that_stays_unread_is_not_stored_with_an_empty_body(monkeypatch, answers, reason):
    """以前读不到就返回空正文，标题照存，存成一个只有标题的版本。现在记为读不到，不存，后面的照读。"""
    a, b, c = (f"https://www.roche.com/media/releases/{x}" for x in "abc")
    batch = _roche_read(monkeypatch, {a: [ROCHE_WHOLE], b: answers, c: [ROCHE_WHOLE]})
    why = f"Roche 详情页读取失败（已重读一次）：{reason}"
    assert batch["unread_items"] == [{"url": b, "reason": why}]
    assert batch["error"]["code"] == "items_unread" and batch["stop_reason"] == "items_unread"
    assert [json.loads(r.raw_content)["url"] for r in batch["records"]] == [a, c]


# ——增量读取（FetchRequest.known_stop）：连续读到“库里已有且没变”的就停——


@pytest.fixture
def stored(monkeypatch):
    """库里已有且没变的原件（记录身份的集合）。真正的判断是 store.records.stored_unchanged，和存版本共用 _adopted_version，
    要真库，见 tests/integration_records.py；这里换成查集合。"""
    from store import records

    known = set()
    monkeypatch.setattr(records, "stored_unchanged", lambda source, item: item.source_record_id in known)
    return known


def _list_site(monkeypatch, urls):
    """nmpa 的列表入口，列表上依次是这些详情页。"""
    class Driver:
        def iter_detail_urls(self):
            yield from urls

    monkeypatch.setattr(site_crawler, "make_driver", lambda site, profile, url: Driver())
    return site_crawler.SITE_PROFILES["nmpa"]["start_urls"][0]


def test_known_stop_stops_after_three_known_in_a_row_and_does_not_download_their_attachments(fake_web, monkeypatch, stored):
    urls = [f"https://www.nmpa.gov.cn/xxgk/ggtg/{i:04d}.html" for i in range(8)]
    stored.update(urls[2:])  # 前两条是新的，从第三条起都读过了
    start = _list_site(monkeypatch, urls)
    batch = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100, known_stop=3))
    assert pages_of(batch) == urls[:5]  # 两条新的 + 连续三条已存的；第 6 条起没有去读
    assert len(batch["records"]) == 2 * 3 + 3  # 新的：页面 + 2 个附件；已存的只有页面，附件上一轮存过，不再下载
    assert len(fake_web["fetch"]) == 5 and len(fake_web["download"]) == 4
    assert batch["stop_reason"] == "reached_known" and batch["next_continuation"] is None and batch["error"] is None


def test_without_known_stop_the_same_list_is_read_to_the_end(fake_web, monkeypatch, stored):
    urls = [f"https://www.nmpa.gov.cn/xxgk/ggtg/{i:04d}.html" for i in range(8)]
    stored.update(urls[2:])
    start = _list_site(monkeypatch, urls)
    batch = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100))  # 手动抓取、回填：known_stop=0
    assert pages_of(batch) == urls and batch["stop_reason"] == "upstream_exhausted"


def test_an_unread_item_resets_the_count_so_known_ones_after_it_do_not_hide_it(fake_web, monkeypatch, stored):
    """没存的不能被“遇到已存的就停”跳过：读不到的这一条打断连续数，后面要再连续三条已存的才停。"""
    urls = [f"https://www.nmpa.gov.cn/xxgk/ggtg/{i:04d}.html" for i in range(8)]
    stored.update(set(urls) - {urls[2]})  # 第 3 条读不到，别的都读过了
    original = site_crawler.fetch_page

    def fetch(url, **kwargs):
        if url == urls[2]:
            raise TimeoutError("synthetic")
        return original(url, **kwargs)

    monkeypatch.setattr(site_crawler, "fetch_page", fetch)
    start = _list_site(monkeypatch, urls)
    batch = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100, known_stop=3))
    assert pages_of(batch) == [urls[0], urls[1], urls[3], urls[4], urls[5]]  # 读不到的没存；停在它后面的第三条已存的
    assert [item["url"] for item in batch["unread_items"]] == [urls[2]]
    assert batch["error"]["code"] == "items_unread" and batch["stop_reason"] == "items_unread"
    assert batch["next_continuation"] is None  # 读到旧的了：没有下一页；运行标“部分完成”，下一次整份读到底（fetch.source_refresh）


def test_a_read_that_stored_nothing_new_is_not_a_failure_but_an_empty_list_is(fake_web, monkeypatch, stored):
    """“解析到了但全是已存的”是正常读到旧的了；“一条都没解析到”才是可能改版，标成失败。"""
    urls = [f"https://www.nmpa.gov.cn/xxgk/ggtg/{i:04d}.html" for i in range(4)]
    stored.update(urls)
    start = _list_site(monkeypatch, urls)
    quiet = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100, known_stop=3))
    assert quiet["error"] is None and quiet["stop_reason"] == "reached_known" and len(pages_of(quiet)) == 3
    start = _list_site(monkeypatch, [])
    broken = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100, known_stop=3))
    assert broken["error"] == {"code": "list_empty", "message": "列表页一条都没解析到，可能改版"}
    assert broken["stop_reason"] == "source_failed" and broken["records"] == []


def test_api_site_stops_at_known_records_and_an_empty_result_is_an_alarm_too(fake_web, monkeypatch, stored):
    from store.record_models import make_uuid as _make_uuid

    items = [{"id": f"R{i}", "t": f"召回{i}", "d": "2026-01-0" + str(i + 1), "u": f"https://x.invalid/{i}"} for i in range(6)]
    profile = {"source_type": "api", "api": {}, "uid_field": "id",
               "field_map": {"title": "t", "publish_date": "d", "source_url": "u"},
               "api_fetcher": lambda cfg: iter(items)}
    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_enforcement", profile)
    stored.update(_make_uuid("fda_api_enforcement:" + item["id"]) for item in items[1:])
    batch = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=100, known_stop=3))
    assert [json.loads(r.raw_content)["raw"]["id"] for r in batch["records"]] == ["R0", "R1", "R2", "R3"]
    assert batch["stop_reason"] == "reached_known" and batch["next_continuation"] is None
    profile["api_fetcher"] = lambda cfg: iter([])
    empty = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=100, known_stop=3))
    assert empty["error"]["code"] == "list_empty" and empty["stop_reason"] == "source_failed"


def test_a_detail_page_that_cannot_be_read_does_not_stop_the_list(fake_web, monkeypatch, stored):
    urls = [f"https://www.nmpa.gov.cn/xxgk/ggtg/{i:04d}.html" for i in range(3)]
    original = site_crawler.fetch_page

    def fetch(url, **kwargs):
        if url == urls[1]:
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")
        return original(url, **kwargs)

    monkeypatch.setattr(site_crawler, "fetch_page", fetch)
    start = _list_site(monkeypatch, urls)
    batch = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100))
    assert pages_of(batch) == [urls[0], urls[2]]  # 第 2 页读不到，没存，第 3 页照读
    assert batch["unread_items"] == [{"url": urls[1], "reason": "TimeoutError：Page.goto: Timeout 30000ms exceeded."}]
    assert batch["error"]["code"] == "items_unread" and "有 1 条读不到" in batch["error"]["message"]


def test_a_stop_while_replaying_to_the_cursor_does_not_move_the_cursor_back(fake_web, monkeypatch):
    """接口站点没有可恢复的游标，续读从头重放到续读位置。重放途中时间到了：位置原样不动（以前倒退到重放走到的地方）。"""
    items = [{"id": f"R{i}", "t": f"召回{i}", "d": "2026-01-01", "u": f"https://x.invalid/{i}"} for i in range(8)]

    def fetcher(cfg):
        for i, item in enumerate(items):
            if i == 2:
                raise transport.Stop("deadline")  # 重放还没走到第 5 条
            yield item

    profile = {"source_type": "api", "api": {}, "uid_field": "id",
               "field_map": {"title": "t", "publish_date": "d", "source_url": "u"}, "api_fetcher": fetcher}
    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_enforcement", profile)
    cursor = {"i": 4, "a": 1, "p": 1}
    cont = {"cursor": json.dumps(cursor), "read_count": 4}
    batch = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=100, continuation=cont))
    assert batch["records"] == [] and batch["stop_reason"] == "deadline" and batch["error"] is None
    assert json.loads(batch["next_continuation"]["cursor"]) == cursor  # 同一个位置，连附件序号也不丢


def test_five_detail_pages_in_a_row_that_cannot_be_read_are_a_site_fault_not_five_unread_items(fake_web, monkeypatch, stored):
    urls = [f"https://www.nmpa.gov.cn/xxgk/ggtg/{i:04d}.html" for i in range(9)]
    original = site_crawler.fetch_page

    def fetch(url, **kwargs):
        if url in urls[1:6]:  # 第 2 到第 6 页连续读不出来
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")
        return original(url, **kwargs)

    monkeypatch.setattr(site_crawler, "fetch_page", fetch)
    start = _list_site(monkeypatch, urls)
    batch = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100))
    assert batch["error"]["code"] == "site_down" and "站点可能出了故障" in batch["error"]["message"]
    assert batch["stop_reason"] == "source_failed" and pages_of(batch) == [urls[0]]  # 读到第 6 页就不再往下读
    assert [item["url"] for item in batch["unread_items"]] == urls[1:6]
    # 中间读到一页就从头数：四条读不到、一条读到、再四条读不到，不算站点故障
    monkeypatch.setattr(site_crawler, "fetch_page", lambda url, **kw: original(url, **kw) if url == urls[4] else fetch_fail(url))

    def fetch_fail(url):
        raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")

    batch = read_public(FetchRequest(source_id="nmpa", query=start, max_records=100))
    assert batch["error"]["code"] == "items_unread" and len(batch["unread_items"]) == 8


def test_budget_known_counts_consecutive_known_only(monkeypatch):
    from store import records

    monkeypatch.setattr(records, "stored_unchanged", lambda source, item: item != "new")
    budget = transport.Budget(FetchRequest(source_id="nmpa", query="by_url", known_stop=3))
    assert [budget.known(item) for item in ("a", "b", "new", "c", "d")] == [True, True, False, True, True]
    assert budget.known(None) is False and budget.known_run == 0  # 没读到的：连续数清零
    with pytest.raises(transport.Stop) as stop:
        for item in ("a", "b", "c"):
            budget.known(item)
    assert stop.value.reason == "reached_known"
    off = transport.Budget(FetchRequest(source_id="nmpa", query="by_url"))
    assert [off.known(item) for item in "abcdef"] == [False] * 6  # known_stop=0：从不停


# ——分组读取（港交所一家公司一组、NCCN 一份指南一组）：续读位置是第几组和组内第几条，“已存的就停”按组算——


def _grouped(monkeypatch, groups, fetched=None):
    """一个接口站点，条目分成几组（groups[g] 是这一组的条目数）。拉取函数和真的一样：从第 start 组读起，每组开头给 GroupStart。
    fetched 记下每次从第几组读起。"""
    from store.record_models import GroupStart

    def fetcher(cfg, start):
        if fetched is not None:
            fetched.append(start)
        for g in range(start, len(groups)):
            yield GroupStart(g)
            for i in range(groups[g]):
                yield {"id": f"g{g}-{i}", "t": f"第{g}组第{i}条", "d": "2026-01-01", "u": f"https://x.invalid/{g}/{i}"}

    profile = {"source_type": "api", "api": {}, "uid_field": "id", "by_group": True,
               "field_map": {"title": "t", "publish_date": "d", "source_url": "u"}, "api_fetcher": fetcher}
    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_enforcement", profile)
    return profile


def _raw_ids(batch):
    return [json.loads(r.raw_content)["raw"]["id"] for r in batch["records"]]


def test_a_grouped_site_goes_on_from_its_group_and_item_without_the_groups_before(fake_web, monkeypatch):
    fetched = []
    _grouped(monkeypatch, [5, 5, 5], fetched)
    first = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=7))
    assert _raw_ids(first) == [f"g0-{i}" for i in range(5)] + ["g1-0", "g1-1"] and first["stop_reason"] == "record_budget"
    assert json.loads(first["next_continuation"]["cursor"]) == {"i": 2, "a": 0, "p": 0, "g": 1}  # 第 1 组的第 2 条
    second = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=100,
                                      continuation=first["next_continuation"]))
    assert _raw_ids(second) == [f"g1-{i}" for i in range(2, 5)] + [f"g2-{i}" for i in range(5)]
    assert second["upstream_pagination_exhausted"] and second["next_continuation"] is None
    assert fetched == [0, 1]  # 第二段从第 1 组读起，没有把第 0 组再读一遍


def test_a_grouped_site_stops_at_known_records_in_each_group_and_goes_on_with_the_next_group(fake_web, monkeypatch, stored):
    from store.record_models import make_uuid as _make_uuid

    _grouped(monkeypatch, [6, 6, 6])
    for g in (0, 1):  # 第 0、1 组：前两条是新的，其余已存；第 2 组全是新的
        stored.update(_make_uuid(f"fda_api_enforcement:g{g}-{i}") for i in range(2, 6))
    batch = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=100, known_stop=3))
    # 每组：两条新的 + 连续三条已存的就停（第 5 条没有去读）；第 2 组读完
    assert _raw_ids(batch) == [f"g{g}-{i}" for g in (0, 1) for i in range(5)] + [f"g2-{i}" for i in range(6)]
    assert batch["stop_reason"] == "upstream_exhausted" and batch["next_continuation"] is None and batch["error"] is None


def test_a_grouped_incremental_read_takes_at_most_group_limit_items_per_group(fake_web, monkeypatch, stored):
    monkeypatch.setattr(site_crawler, "GROUP_LIMIT", 2)
    _grouped(monkeypatch, [5, 5])
    batch = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=100, known_stop=3))
    assert _raw_ids(batch) == ["g0-0", "g0-1", "g1-0", "g1-1"]  # 更老的交给回填
    backfill = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=100))
    assert len(backfill["records"]) == 10  # 回填（known_stop=0）不受限


def test_a_group_that_cannot_be_read_does_not_stop_the_next_group_and_a_stop_while_asking_for_a_group_keeps_that_group(
        fake_web, monkeypatch):
    from store.record_models import GroupStart, ItemUnreadable

    def fetcher(cfg, start):
        yield GroupStart(0)
        yield ItemUnreadable("https://x.invalid/company/0", "读取失败：TimeoutError")
        yield GroupStart(1)
        yield {"id": "g1-0", "t": "一", "d": "2026-01-01", "u": "https://x.invalid/1/0"}
        yield GroupStart(2)
        raise transport.Stop("deadline")  # 要第 2 组的数据时时间到了

    profile = {"source_type": "api", "api": {}, "uid_field": "id", "by_group": True,
               "field_map": {"title": "t", "publish_date": "d", "source_url": "u"}, "api_fetcher": fetcher}
    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_enforcement", profile)
    batch = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=100))
    assert _raw_ids(batch) == ["g1-0"] and batch["unread_items"][0]["url"] == "https://x.invalid/company/0"
    assert batch["stop_reason"] == "deadline"
    assert json.loads(batch["next_continuation"]["cursor"]) == {"i": 0, "a": 0, "p": 0, "g": 2}  # 下一段从第 2 组读起


def test_a_grouped_site_that_gives_no_item_at_all_is_the_empty_list_alarm_but_an_empty_tail_is_not(fake_web, monkeypatch):
    _grouped(monkeypatch, [0, 0, 0])
    first = FetchRequest(source_id="fda_api_enforcement", query="records")
    assert read_public(first)["error"]["code"] == "list_empty"
    cont = {"cursor": json.dumps({"i": 0, "a": 0, "p": 0, "g": 1}), "read_count": 1}
    tail = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", continuation=cont))
    assert tail["error"] is None and tail["upstream_pagination_exhausted"]
