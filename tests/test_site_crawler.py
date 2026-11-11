"""站点插件 collection.site_crawler（NMPA / CDE / ChiCTR 页面、接口站点、表格页）。

不联网：页面抓取、解析和附件下载都换成替身；旧代码的站点配置和调用顺序原样复用。
"""

import json

import pytest

from biz_rules.history_models import ArchiveRecord, FetchRequest, HistoryError
from biz_rules.history_sources import PLUGINS, project_record, read_public
from collection import catalog, rate, site_crawler, transport

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
        transport.gate(url)  # 替身也是一次真实传输：过闸、计数、计字节
        transport.received(len(FakePage.html_content.encode()))
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

    def download(pdf_url, parent_uuid="", source_site="", force=False, cookies=None):
        transport.gate(pdf_url)
        calls["download"].append((pdf_url, cookies))
        if pdf_url == ATT2 and calls.get("fail_second"):
            raise RuntimeError("HTTP 404")
        path = tmp_path / (pdf_url[-6:])
        path.write_bytes(b"\xd0\xcf\x11\xe0 synthetic" + pdf_url.encode())
        return {"uuid": "f", "local_path": str(path), "source_url": pdf_url, "file_ext": "." + pdf_url.rsplit(".", 1)[1],
                "skipped": False}

    monkeypatch.setattr(site_crawler, "_fetch_page", fetch_page)
    monkeypatch.setattr(site_crawler, "_route_webpage", route_webpage)
    monkeypatch.setattr(site_crawler, "_download", download)
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
        assert source in catalog.FETCHABLE and source not in catalog.WATCHABLE
        assert spec.verified is False  # 迁入旧代码，本系统未对原站实测



def test_read_public_dispatches_to_the_migrated_plugin(monkeypatch):
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setattr(site_crawler, "read_site", lambda request: {"marker": request.source_id})
    assert "site_crawler" in PLUGINS
    batch = read_public(FetchRequest(source_id="chictr", query="by_url",
                                     source_record_ids=["https://www.chictr.org.cn/showproj.html?proj=1"]))
    assert batch["marker"] == "chictr" and batch["requests"] == 0  # 计量来自同一份预算


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
        {"url": "u4"}, {"text": "没有网址"}, None]}
    assert site_crawler._links(parsed) == [
        {"url": "u1", "text": "从 name 读"}, {"url": "u2", "text": "从 text 读"}, {"url": "u3", "text": "text"},
        {"url": "u4", "text": ""}]
    assert site_crawler._links({}) == [] and site_crawler._links({"attachment_links": None}) == []


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
    monkeypatch.setattr(site_crawler, "_make_driver", lambda site, profile, url: Driver())
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
    monkeypatch.setattr(site_crawler, "_make_driver", lambda site, profile, url: Driver())
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


def test_request_budget_counts_every_request(fake_web):
    urls = [PAGE, PAGE.replace("123.html", "124.html")]
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=urls, max_pages=1,
                                     max_records=100))
    # max_pages=1 → 10 个请求：两页各 1 个页面请求 + 2 个附件 = 6，没用完
    assert batch["requests"] == 6 and batch["upstream_pagination_exhausted"]


def test_byte_budget_stops_and_keeps_what_was_read(fake_web, monkeypatch):
    # 第一页放得下，第二页读到一半超出：第二页不保留（读不完整），第一页和它的附件保留，续读指向第二页
    monkeypatch.setenv("DATAFABRIC_READ_MAX_BYTES", str(len(FakePage.html_content.encode()) * 3 // 2))
    urls = [PAGE, PAGE.replace("123.html", "124.html")]
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=urls, max_records=100))
    assert batch["stop_reason"] == "byte_budget" and pages_of(batch) == [PAGE]
    assert batch["next_continuation"] is not None


def test_too_large_attachment_is_a_recorded_gap(fake_web, monkeypatch):
    def too_large(*a, **k):
        raise ValueError("attachment_too_large")

    monkeypatch.setattr(site_crawler, "_download", too_large)
    batch = read_public(FetchRequest(source_id="nmpa", query="by_url", source_record_ids=[PAGE]))
    assert {a["status"] for a in batch["attachment_coverage"]} == {"too_large"} and len(batch["records"]) == 1


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
    with pytest.raises(HistoryError):
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
    from core_engine.api_router import _make_uuid
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


def test_every_real_http_transfer_is_gated_and_budgeted_before_it_is_sent(monkeypatch):
    """Codex r69 反例：拉取函数在产出第一条记录前发 12 个请求。现在每个请求发出前都过共享限速并计数，
    请求预算 10 次用完就停在第 11 个请求之前，不再读到底。"""
    import httpx

    seen = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: (seen.append("http"), httpx.Response(200, json={}))[1]))

    def fetcher(config):
        for i in range(12):
            client.get(f"https://synthetic.invalid/record/{i}")
        yield {"id": "one", "title": "Synthetic result"}

    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_drug_label",
                        {"source_type": "api", "api_fetcher": fetcher, "api": {}, "field_map": {"title": "title"},
                         "uid_field": "id"})
    monkeypatch.setattr(rate, "wait", lambda *a, **k: seen.append("gate"))
    result = site_crawler.read_site(FetchRequest(source_id="fda_api_drug_label", query="records", max_pages=1,
                                                 max_records=1))
    assert seen[:4] == ["gate", "http", "gate", "http"]  # 先过闸再发
    assert seen.count("http") == 10 and result["requests"] == 10
    assert result["stop_reason"] == "request_budget" and not result["upstream_pagination_exhausted"]
    assert result["records"] == []


def test_transient_errors_are_retried_at_the_transport_and_each_retry_is_counted(monkeypatch):
    import httpx

    from config.settings import APP

    monkeypatch.setattr(APP, "TASK_RETRIES", 2)
    monkeypatch.setattr(APP, "TASK_RETRY_DELAY", 0.0)
    answers = iter([httpx.Response(429), httpx.Response(200, json={"ok": 1})])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(answers)))

    def fetcher(config):
        assert client.get("https://synthetic.invalid/x").status_code == 200
        yield {"id": "one", "title": "ok"}

    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_drug_label",
                        {"source_type": "api", "api_fetcher": fetcher, "api": {}, "field_map": {"title": "title"},
                         "uid_field": "id"})
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    result = site_crawler.read_site(FetchRequest(source_id="fda_api_drug_label", query="records", max_records=5))
    assert result["requests"] == 2 and len(result["records"]) == 1


def test_transfers_outside_a_read_are_not_gated(monkeypatch):
    import httpx

    calls = []
    monkeypatch.setattr(rate, "wait", lambda *a, **k: calls.append("gate"))
    transport.install()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    client.get("https://synthetic.invalid/")
    assert calls == []


def test_list_pages_fetched_in_the_driver_thread_go_through_the_gate(fake_web, monkeypatch):
    """翻页驱动在独立线程里取列表页：线程带着读取上下文，列表页同样过闸计数；驱动自己的间隔归零。"""
    import concurrent.futures

    from collection.transport import in_context

    class Driver:
        delay = 2.0

        def iter_detail_urls(self):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(in_context(lambda: transport.gate(PAGE))).result()  # 列表页的一次真实传输
            yield PAGE

    made = []
    import core_engine.pagination_driver as pd

    monkeypatch.setattr(pd, "make_driver", lambda site, profile, url: made.append(Driver()) or made[-1])
    start = site_crawler.SITE_PROFILES["nmpa"]["start_urls"][0]
    batch = read_public(FetchRequest(source_id="nmpa", query=start))
    assert made[0].delay == 0 and batch["requests"] == 4  # 列表页 1 + 详情页 1 + 附件 2


def test_transient_errors_are_retried_and_others_are_not(monkeypatch):
    import httpx

    from config.settings import APP

    monkeypatch.setattr(APP, "TASK_RETRIES", 2)
    monkeypatch.setattr(APP, "TASK_RETRY_DELAY", 0.01)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setattr(transport.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("t")
        return "ok"

    budget = _budget()
    assert transport._hop(budget, "https://synthetic.invalid/", flaky, drain=lambda r: None) == "ok"
    assert len(calls) == 3 and budget.requests == 3  # 每次重发都重新过闸

    calls.clear()

    def broken():
        calls.append(1)
        raise ValueError("parse error")

    with pytest.raises(ValueError):
        transport._hop(_budget(), "https://synthetic.invalid/", broken, drain=lambda r: None)
    assert len(calls) == 1
    assert transport.is_transient(httpx.ReadTimeout("t")) and not transport.is_transient(RuntimeError("HTTP 429"))


def _budget():
    return transport.Budget(max_requests=10, max_bytes=10**9, max_records=10, deadline_seconds=60)


def _api_site(monkeypatch, fetcher):
    monkeypatch.setitem(site_crawler.SITE_PROFILES, "fda_api_drug_label",
                        {"source_type": "api", "api_fetcher": fetcher, "api": {}, "field_map": {"title": "title"},
                         "uid_field": "id"})


def test_codex_401_every_redirect_hop_is_metered(monkeypatch):
    """Codex 401 反例：httpx 自动跟随 12 跳重定向，预算 10。现在每一跳在实际发送处过闸、计数，第 11 跳前停下。"""
    import httpx

    hops, gates = [], []

    def handler(request):
        hops.append(str(request.url))
        if len(hops) < 12:
            return httpx.Response(302, headers={"Location": "https://synthetic.invalid/hop/" + str(len(hops))})
        return httpx.Response(200, json={"done": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    def fetcher(config):
        client.get("https://synthetic.invalid/start")
        yield {"id": "one", "title": "synthetic redirected record"}

    _api_site(monkeypatch, fetcher)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: gates.append(True))
    batch = site_crawler.read_site(FetchRequest(source_id="fda_api_drug_label", query="records", max_pages=1,
                                                max_records=1))
    assert len(hops) == 10 and len(gates) == 10 and batch["requests"] == 10
    assert batch["stop_reason"] == "request_budget" and batch["records"] == []


def test_codex_402_retry_response_bytes_count_towards_the_budget(monkeypatch):
    """Codex 402 第 3 点：两次 429 各回 2000 字节，预算 1000。第一次 429 的响应体就用完预算，不再重发。"""
    import httpx

    bodies = []

    def handler(request):
        status, body = (429, b"x" * 2000) if len(bodies) < 2 else (200, b"ok")
        bodies.append(len(body))
        return httpx.Response(status, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def fetcher(config):
        client.get("https://synthetic.invalid/rate-limited")
        yield {"id": "one", "title": "synthetic record"}

    _api_site(monkeypatch, fetcher)
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    monkeypatch.setenv("DATAFABRIC_READ_MAX_BYTES", "1000")
    monkeypatch.setattr(transport, "_retry_settings", lambda: (2, 0.0))
    batch = site_crawler.read_site(FetchRequest(source_id="fda_api_drug_label", query="records", max_pages=1,
                                                max_records=1))
    assert len(bodies) == 1 and batch["stop_reason"] == "byte_budget" and batch["bytes"] == 2000


def test_streamed_download_stops_at_the_chunk_that_exhausts_the_budget(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, content=iter([b"a" * 400] * 5))  # 流式 2000 字节

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)
    budget = transport.Budget(max_requests=10, max_bytes=1000, max_records=10, deadline_seconds=60)
    seen = []
    with transport.metered(budget), pytest.raises(transport.Stop):
        with client.stream("GET", "https://synthetic.invalid/file") as response:
            for chunk in response.iter_bytes():
                seen.append(len(chunk))
    assert sum(seen) <= 1000 < budget.bytes <= 1200 and budget.stopped == "byte_budget"


def test_requests_and_curl_redirects_are_gated_per_hop(monkeypatch):
    """requests：每一跳调用一次 HTTPAdapter.send；curl_cffi：关掉库内部跳转，由闸门逐跳跟随。"""
    import requests

    gates = []
    monkeypatch.setattr(rate, "wait", lambda group, *a, **k: gates.append(group))

    class Raw:
        def __init__(self, body):
            self.body = body

        def stream(self, *a, **k):
            yield self.body

        def read(self, *a, **k):
            body, self.body = self.body, b""
            return body

        def release_conn(self):
            pass

        def close(self):
            pass

    sent = []

    def adapter_send(self, request, *args, **kwargs):
        sent.append(request.url)
        response = requests.Response()
        response.request, response.url, response.raw = request, request.url, Raw(b"x" * 10)
        if len(sent) < 3:
            response.status_code, response.headers["Location"] = 302, "https://b.invalid/" + str(len(sent))
        else:
            response.status_code = 200
        return response

    from requests.adapters import HTTPAdapter

    monkeypatch.setattr(HTTPAdapter, "send", adapter_send)
    with transport.metered(_budget()) as budget:
        assert requests.get("https://a.invalid/start").status_code == 200
    assert len(sent) == 3 and budget.requests == 3 and budget.bytes == 30
    assert gates == ["a.invalid", "b.invalid", "b.invalid"]  # 按每一跳的实际主机限速

    curl = pytest.importorskip("curl_cffi.requests")
    calls = []

    class Answer:
        def __init__(self, status, location=None):
            self.status_code, self.content = status, b"yy"
            self.headers = {"location": location} if location else {}

    def curl_request(self, method, url, *args, **kwargs):
        calls.append((url, kwargs.get("allow_redirects")))
        return Answer(302, "/next") if len(calls) == 1 else Answer(200)

    monkeypatch.setattr(curl.Session, "request", curl_request)
    with transport.metered(_budget()) as budget:
        assert curl.Session().request("GET", "https://c.invalid/a").status_code == 200
    assert calls == [("https://c.invalid/a", False), ("https://c.invalid/next", False)]
    assert budget.requests == 2 and budget.bytes == 4


def test_drugbank_without_an_api_key_is_a_failed_read_that_says_so(monkeypatch):
    """No key used to end the fetcher silently, which read as "source read to the end, nothing there"."""
    monkeypatch.delenv("DRUGBANK_API_KEY", raising=False)
    monkeypatch.setitem(site_crawler.SITE_PROFILES["drugbank"]["api"], "api_key", "")
    batch = read_public(FetchRequest(source_id="drugbank", query="records"))
    assert batch["error"]["code"] == "source_not_configured" and "没配 DrugBank API Key" in batch["error"]["message"]
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]
    assert batch["records"] == []


def test_failed_site_read_keeps_the_real_reason_without_secrets(fake_web, monkeypatch):
    """The reason used to be the class name only ("站点读取失败：TimeoutError"). Now it is the first line of the
    error message; the rest of the browser's call log, URL query strings and key=value pairs stay out."""
    class Driver:
        def iter_detail_urls(self):
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating to "
                               '"https://www.nmpa.gov.cn/x?api_key=SECRET123", waiting until "load"')
            yield

    start = site_crawler.SITE_PROFILES["nmpa"]["start_urls"][0]
    monkeypatch.setattr(site_crawler, "_make_driver", lambda site, profile, url: Driver())
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
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: answer())
    batch = read_public(FetchRequest(source_id=source, query="records"))
    assert batch["error"]["code"] == "source_read_failed"
    assert reason in batch["error"]["message"] and "已读 0 条" in batch["error"]["message"]
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]
    assert batch["records"] == []


def test_openfda_failure_on_a_later_page_keeps_what_was_read_and_still_fails(monkeypatch):
    """第二页出错：第一页读到的原件保留，但这次读取不是“读完了”。"""
    import httpx
    from biz_rules.extractors.fda import _openfda_base

    record = {"set_id": "S1", "effective_time": "20261101", "openfda": {},
              "spl_product_data_elements": ["Some Product SOME INGREDIENT"]}
    link = '<https://api.fda.gov/drug/label.json?search_after=1&sort=effective_time:desc>; rel="next"'
    answers = iter([_http(200, {"meta": {"results": {"total": 3}}, "results": [record]}, {"Link": link}),
                    _http(500, {"error": {"code": "SERVER_ERROR"}})])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: next(answers))
    monkeypatch.setattr(_openfda_base, "REQUEST_DELAY", 0)
    batch = read_public(FetchRequest(source_id="fda_api_drug_label", query="records", max_records=5))
    assert len(batch["records"]) == 1
    assert "HTTP 500" in batch["error"]["message"] and "已读 1 条" in batch["error"]["message"]
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]


def test_openfda_404_past_the_last_skip_page_is_still_the_normal_end(monkeypatch):
    """skip 翻页翻过最后一页，openFDA 回 404：这是读完了，不是失败。"""
    import httpx
    from biz_rules.extractors.fda import _openfda_base

    record = {"recall_number": "D-1-2026", "product_description": "Some product", "report_date": "20261101"}
    answers = iter([_http(200, {"meta": {"results": {"total": 5}}, "results": [record]}),
                    _http(404, {"error": {"code": "NOT_FOUND", "message": "No matches found!"}})])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: next(answers))
    monkeypatch.setattr(_openfda_base, "REQUEST_DELAY", 0)
    batch = read_public(FetchRequest(source_id="fda_api_enforcement", query="records", max_records=10))
    assert len(batch["records"]) == 1 and batch["error"] is None
    assert batch["upstream_pagination_exhausted"] and batch["stop_reason"] == "upstream_exhausted"


@pytest.mark.parametrize(("answer", "reason"), [
    (lambda: _http(401, {"error": "Unauthorized"}), "HTTP 401"),
    (lambda: (_ for _ in ()).throw(__import__("httpx").ReadTimeout("read timed out")), "ReadTimeout"),
])
def test_drugbank_http_errors_and_timeouts_are_failed_reads_without_the_key(monkeypatch, answer, reason):
    import httpx

    monkeypatch.setitem(site_crawler.SITE_PROFILES["drugbank"]["api"], "api_key", "test-key-0000")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: answer())
    batch = read_public(FetchRequest(source_id="drugbank", query="records"))
    assert batch["error"]["code"] == "source_read_failed" and f"DrugBank 读取失败：{reason}" in batch["error"]["message"]
    assert "test-key-0000" not in batch["error"]["message"]
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]


class _RochePage:
    """Roche 列表页替身：只有状态码，页面上没有新闻条目。"""
    def __init__(self, status):
        self.status = status

    def css(self, selector):
        return []

    def find_all(self, matcher):
        return []


def _roche_one_year(monkeypatch, fetch):
    from biz_rules.extractors.roche import roche_web_fetcher

    monkeypatch.setitem(site_crawler.SITE_PROFILES["roche"]["api"], "year_end", 2026)  # 只读 2026 年
    monkeypatch.setattr(roche_web_fetcher.StealthyFetcher, "fetch", staticmethod(fetch))
    return read_public(FetchRequest(source_id="roche", query="records"))


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
    ended = _roche_one_year(monkeypatch, lambda url, **kw: _RochePage(200))  # 这一年没有（更多）新闻：正常读完
    assert ended["error"] is None and ended["upstream_pagination_exhausted"]


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


def _fierce_read(monkeypatch, pages):
    """pages：文章网址 → 依次给出的答复（页面替身，或要抛出的异常）。RSS 里的标题是空的，和真实情况一致。"""
    from biz_rules.extractors.fiercepharma import fiercepharma_fetcher as fetcher

    monkeypatch.setattr(fetcher, "REQUEST_DELAY", 0)
    monkeypatch.setattr(fetcher, "RETRY_DELAY", 0)
    monkeypatch.setattr(fetcher, "_fetch_rss_items", lambda cfg: [
        {"url": url, "guid": "", "title": "", "description": "RSS 摘要", "pub_date": "2026-11-01"} for url in pages])

    def fetch(url, **kwargs):
        answer = pages[url].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(fetcher.StealthyFetcher, "fetch", staticmethod(fetch))
    return read_public(FetchRequest(source_id="fiercepharma", query="records", max_records=5))


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
def test_fiercepharma_article_that_stays_unread_is_not_stored_and_fails_the_read(monkeypatch, answers, reason):
    a, b, c = (f"https://www.fiercepharma.com/pharma/{x}" for x in "abc")
    batch = _fierce_read(monkeypatch, {a: [WHOLE], b: answers, c: [WHOLE]})
    assert batch["error"] == {"code": "source_read_failed",
                              "message": f"FiercePharma 详情页读取失败（已重读一次）：{reason} | {b}"}
    assert batch["stop_reason"] == "source_failed" and not batch["upstream_pagination_exhausted"]
    assert [json.loads(r.raw_content)["url"] for r in batch["records"]] == [a]  # 前面读全的保留，坏的这篇和后面的没存
