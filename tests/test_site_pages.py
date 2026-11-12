"""站点配置里的选择器和提取函数，对着真实页面的样本检查（不联网）。

tests/fixtures/site_pages/ 里的 HTML 是 2026-11-11 从真实页面存下来的，只截取选择器用到的那一块，内容没改。
站点改版后这些测试不会自己发现（样本是旧的），所以改选择器时要先看真实页面、再换样本。
"""

from pathlib import Path

from scrapling.parser import Selector

from config.crawler_settings import SITE_PROFILES

PAGES = Path(__file__).parent / "fixtures" / "site_pages"


def page(name):
    return Selector((PAGES / name).read_text(encoding="utf-8"))


# ── CDE 指导原则（cde_zdyz）──────────────────────────────────────────────

def test_cde_zdyz_list_is_a_table_whose_rows_link_to_the_detail_pages():
    """列表页改成了表格：每行一份指导原则，标题是 /zdyz/domesticinfopage?zdyzIdCODE=… 的链接。"""
    profile = SITE_PROFILES["cde_zdyz"]
    sample = page("cde_zdyz_list.html")
    links = sample.css(profile["list_item_selector"])
    assert len(links) == 10
    assert all(a.attrib["href"].startswith("/zdyz/domesticinfopage?zdyzIdCODE=") for a in links)
    assert len({a.attrib["href"] for a in links}) == 10
    assert sample.css(profile["wait_selector"])


def test_cde_zdyz_detail_gives_title_date_info_table_and_the_attachment_link():
    """详情页只有一张信息表，指导原则正文在附件 PDF 里：标题和日期来自 meta，附件来自 /zdyz/downloadAtt 链接。"""
    profile = SITE_PROFILES["cde_zdyz"]
    sample = page("cde_zdyz_detail.html")
    assert sample.css(profile["detail_wait_selector"])
    got = profile["detail_extractor"](sample)
    title = "药物临床试验安全信息报告与风险沟通交流技术指导原则"
    assert got["title"] == title and got["publish_date"] == "2026-11-09"
    assert f"名称 {title}" in got["body_text"] and "适用范围 化学药,中药,生物制品" in got["body_text"]
    assert "意见留言" not in got["body_text"]  # 留言框不是内容
    assert got["attachment_links"] == [{
        "name": title + ".pdf",
        "url": "https://www.cde.org.cn/zdyz/downloadAtt?idCODE=2a6418f49c904572edf65cbf4497b093"}]


def test_cde_detail_extractor_still_reads_the_older_page_layout():
    """cde / cde_xxgk 用同一个提取函数：正文区 div.new_detail_content 和 div.relatedNews 里的附件照旧。"""
    from biz_rules.extractors.cde import extract_cde_detail

    html = ("<html><head><meta name='ArticleTitle' content='某通告'><meta name='PubDate' content='20260403'></head><body>"
            "<div class='new_detail_content'><p>正文一段。</p></div>"
            "<div class='relatedNews'><a href='/main/att/download/x1'>附件一.pdf</a></div></body></html>")
    got = extract_cde_detail(Selector(html))
    assert got["title"] == "某通告" and got["publish_date"] == "2026-04-03" and got["body_text"] == "正文一段。"
    assert got["attachment_links"] == [{"name": "附件一.pdf", "url": "https://www.cde.org.cn/main/att/download/x1"}]


# ── FDA 新闻稿（fda_press）──────────────────────────────────────────────

def test_fda_press_starts_at_the_newsroom_address_and_lists_links_without_the_old_heading_class():
    """旧起始地址 /news-events/press-announcements 已 404，列表页搬到 fda-newsroom 下；页面上没有 h3.lcds-text-heading，
    列表是 div.view-content 里的 li > a（链接文字是“日期 - 标题”），详情页地址仍在 /news-events/press-announcements/ 下。"""
    profile = SITE_PROFILES["fda_press"]
    assert profile["start_urls"] == ["https://www.fda.gov/news-events/fda-newsroom/press-announcements"]
    sample = page("fda_press_list.html")
    links = sample.css(profile["list_item_selector"])
    assert len(links) == 10 and sample.css(profile["wait_selector"])
    assert all(a.attrib["href"].startswith("/news-events/press-announcements/") for a in links)
    assert links[0].get_all_text(strip=True).startswith("November 9, 2026")


# ── FDA 药品安全通讯（fda_drug_safety）───────────────────────────────────

def test_fda_drug_safety_list_is_one_static_table_read_with_a_plain_request_in_a_single_page():
    """整张表（日期 | 标题，共约 44 条）都在静态 HTML 里（浏览器里才被分成每页 10 行），标题是详情页链接，最新的在前：
    用简单请求读一页就够了，不开浏览器（浏览器开一个页面要 20-60 秒）。fixture 是 2026-11-12 存下来的整张表。"""
    from core_engine.pagination_driver import make_driver

    profile = SITE_PROFILES["fda_drug_safety"]
    sample = page("fda_drug_safety_list.html")
    links = sample.css(profile["list_item_selector"])
    assert len(links) == 44 and len({a.attrib["href"] for a in links}) == 44
    assert all(a.attrib["href"].startswith("/drugs/") for a in links)
    assert sample.css("table tbody td")[0].get_all_text(strip=True) == "11-9-2026"
    assert profile["use_simple_fetch"] is True
    driver = make_driver("fda_drug_safety", profile, profile["start_urls"][0])
    fetched = []
    driver._fetch = lambda url: (fetched.append(url), {"page": sample})[1]
    urls = list(driver.iter_detail_urls())
    assert fetched == profile["start_urls"] and len(urls) == 44  # 一页，没有“下一页”
    assert urls[0].startswith("https://www.fda.gov/drugs/drug-safety-communications/fda-requires-safety-labeling")


def test_fda_drug_safety_issue_date_comes_from_the_page_text_not_from_the_content_current_as_of_footer():
    """页脚 <time datetime> 是“Content current as of”（最后一次改页面的日期），不是发布日期：以前 44 篇里一批是同一天
    2026-04-03。发布日期在正文上方的小标题（月-日-年）或标题下的副标题（Month D, YYYY）里，和列表里的日期一致。"""
    from biz_rules.extractors.fda.drug_safety import extract_fda_drug_safety

    for name, issued, footer in [("subtitle_date", "2026-11-09", None), ("panels", "2026-03-31", "04/03/2026"),
                                 ("plain", "2022-06-01", "06/07/2022")]:
        sample = page(f"fda_drug_safety_detail_{name}.html")
        parsed = extract_fda_drug_safety(sample)
        assert parsed["publish_date"] == issued and parsed["title"]
        if footer:  # 样本里带着这个误导的页脚日期
            assert sample.css("time")[0].get_all_text(strip=True) == footer


def test_fda_drug_safety_body_is_the_whole_article_column_also_on_pages_without_accordion_panels():
    """文章在 div[role=main] 里。老页面没有折叠面板（div.panel-group），旧选择器匹配到空元素，入库的正文是空的。"""
    from biz_rules.extractors.fda.drug_safety import extract_fda_drug_safety

    plain = extract_fda_drug_safety(page("fda_drug_safety_detail_plain.html"))
    assert "Ukoniq" in plain["body_text"] and "withdrawn its approval" in plain["body_text"]
    panels = extract_fda_drug_safety(page("fda_drug_safety_detail_panels.html"))
    assert "What Safety Concern" in panels["body_text"] or "What Is FDA Doing" in panels["body_text"]


# ── 港交所披露易（hkex）──────────────────────────────────────────────────

# 接口 titleSearchServlet.do 返回的三行真实数据：普通公告（和黄医药 00013）；“展示文件”（只是一句提示语，各公司指向同一个网址）；
# 标题里带 HTML 转义（&#x2f; 是斜杠）的公告（中国生物制药 01177）。“SHORT_TEXT”是页面上的文件类型栏，“TITLE”才是公告标题。
HKEX_ROWS = [
    {"FILE_INFO": "200KB", "NEWS_ID": "12400954", "SHORT_TEXT": "公告及通告 - [海外監管公告-業務發展最新情況]<br/>",
     "DOD_WEB_PATH": "", "STOCK_NAME": "和黃醫藥", "FILE_TYPE": "PDF", "DATE_TIME": "05/11/2026 08:00",
     "TITLE": "自願性公告 - 和黃醫藥宣佈索樂匹尼布 (sovleplenib) 獲FDA孤兒藥資格認定用於治療自身免疫性溶血性貧血",
     "STOCK_CODE": "00013", "FILE_LINK": "/listedco/listconews/sehk/2026/1105/2026110500118_c.pdf"},
    {"FILE_INFO": "1KB", "NEWS_ID": "12099592", "SHORT_TEXT": "展示文件<br/>", "DOD_WEB_PATH": "",
     "STOCK_NAME": "和黃醫藥", "FILE_TYPE": "HTM", "DATE_TIME": "09/04/2026 16:44",
     "TITLE": "發行人剛在本網站英文版面發布一項公告，相應中文版本或會／或不會在本版面發布。",
     "STOCK_CODE": "00013", "FILE_LINK": "/listedco/listconews/sehk/dod/tc.html"},
    {"FILE_INFO": "176KB", "NEWS_ID": "12367210", "SHORT_TEXT": "公告及通告 - [其他-業務發展最新情況]<br/>",
     "DOD_WEB_PATH": "", "STOCK_NAME": "中國生物製藥", "FILE_TYPE": "PDF", "DATE_TIME": "08/10/2026 17:30",
     "TITLE": "自願公告 - TQC3721吸入混懸液「PDE3&#x2f;4抑制劑」III期註冊臨床完成全部受試者入組",
     "STOCK_CODE": "01177", "FILE_LINK": "/listedco/listconews/sehk/2026/1008/2026100800670_c.pdf"},
]
HKEX_COMPANIES = [{"code": "00013", "name": "和黃醫藥", "stock_id": 1000001102},
                  {"code": "01177", "name": "中國生物製藥", "stock_id": 6833}]


def _hkex_stub(monkeypatch, overrides=None, rows_of=None):
    """用替身接口顶掉 hkex 的 requests.get，返回 (站点接口配置, 发出的请求参数)。rows_of(stock_id) 给这家公司的行；
    默认 00013 给前两行、01177 给第三行；返回异常对象就是这家读不到。"""
    import json

    from biz_rules.extractors import hkex_fetcher

    sent = []
    rows_of = rows_of or {1000001102: HKEX_ROWS[:2], 6833: HKEX_ROWS[2:]}.get

    def get(url, params, **kwargs):
        sent.append(params)
        rows = rows_of(params["stockId"])
        if isinstance(rows, Exception):
            raise rows

        class Reply:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": json.dumps(rows), "hasNextRow": False, "recordCnt": len(rows)}

        return Reply()

    monkeypatch.setattr(hkex_fetcher.requests, "get", get)
    return {**SITE_PROFILES["hkex"]["api"], "companies": HKEX_COMPANIES, **(overrides or {})}, sent


def _hkex_read(monkeypatch, overrides=None, rows_of=None):
    """用替身接口读一遍 hkex 的拉取函数，返回 (读到的公告, 发出的请求参数)。"""
    from biz_rules.extractors import hkex_fetcher

    api, sent = _hkex_stub(monkeypatch, overrides, rows_of)
    return list(hkex_fetcher.fetch_hkex_announcements(api)), sent


def test_hkex_end_date_defaults_to_today_and_is_sent_as_yyyymmdd(monkeypatch):
    """截止日写死成 20260410 时，之后发布的公告全读不到。留空 = 读取当天；接口不接受空的 to（返回 0 条），所以不能原样传空。
    rowRange 要传接口的上限 10000：一次请求读完这家公司从 from_date 起的全部公告（默认只给 100 条）。"""
    from datetime import date

    assert SITE_PROFILES["hkex"]["api"]["to_date"] == ""
    _, sent = _hkex_read(monkeypatch)
    assert sent[0]["toDate"] == date.today().strftime("%Y%m%d") and sent[0]["fromDate"] == "20240101"
    assert sent[0]["rowRange"] == 10000 and len(sent) == 2  # 两家公司各一次请求
    _, sent = _hkex_read(monkeypatch, {"to_date": "20261231"})
    assert sent[0]["toDate"] == "20261231"


def test_hkex_skips_the_display_document_notice_rows(monkeypatch):
    """“展示文件”行只是提示语，不是公告：不读。普通公告照常读到。"""
    announcements, _ = _hkex_read(monkeypatch)
    assert [a["stock_code"] for a in announcements] == ["00013", "01177"]
    assert announcements[0]["pdf_url"].endswith("/2026/1105/2026110500118_c.pdf")
    kept, _ = _hkex_read(monkeypatch, {"skip_headlines": []})
    assert len(kept) == 3


def test_hkex_title_is_the_announcement_title_and_the_file_type_column_is_kept_apart(monkeypatch):
    """以前标题取的是“文件类型”栏（“公告及通告 - [...]”“月報表”），真正的公告标题在文件链接的文字里。
    标题里的 HTML 转义要还原；日期是“日/月/年 时:分”，要转成 YYYY-MM-DD。"""
    first, second = _hkex_read(monkeypatch)[0]
    assert first["title"].startswith("自願性公告 - 和黃醫藥宣佈索樂匹尼布")
    assert first["doc_type"] == "公告及通告 - [海外監管公告-業務發展最新情況]"
    assert second["title"] == "自願公告 - TQC3721吸入混懸液「PDE3/4抑制劑」III期註冊臨床完成全部受試者入組"
    assert (first["publish_date"], second["publish_date"]) == ("2026-11-05", "2026-10-08")


def test_hkex_record_through_the_site_config_has_title_date_and_the_pdf_as_attachment(monkeypatch):
    """走站点配置的字段映射之后：标题是公告标题、日期是 ISO、公告 PDF 作为附件交给采集插件。"""
    from core_engine.api_router import to_result

    first = _hkex_read(monkeypatch)[0][0]
    parsed, kept = to_result("hkex", SITE_PROFILES["hkex"], first)
    assert parsed["title"] == first["title"] and parsed["publish_date"] == "2026-11-05"
    assert parsed["attachment_links"] == [{"url": first["pdf_url"], "name": first["title"]}]
    assert kept is first


def test_hkex_company_that_cannot_be_read_is_given_as_unreadable_with_its_name_and_the_next_one_is_still_read(monkeypatch):
    """一家读不到（网络、状态码）：给一个 ItemUnreadable，写明是哪家、为什么，采集插件记下它、不存版本；
    接着读下一家，不是整个来源停在这里。"""
    from biz_rules.history_models import ItemUnreadable

    def rows_of(stock_id):
        return TimeoutError("read timed out") if stock_id == 1000001102 else HKEX_ROWS[2:]

    unreadable, announcement = _hkex_read(monkeypatch, rows_of=rows_of)[0]
    assert isinstance(unreadable, ItemUnreadable) and "stockId=1000001102" in unreadable.url
    assert "和黃醫藥" in unreadable.reason and "00013" in unreadable.reason and "TimeoutError" in unreadable.reason
    assert announcement["stock_code"] == "01177"


def test_hkex_a_joint_announcement_under_two_companies_is_given_once(monkeypatch):
    """联合公告会出现在几家公司名下，指向同一个 PDF：同一个文件只给一次，不然同一条记录一次读两遍。"""
    rows = {1000001102: HKEX_ROWS[:1], 6833: HKEX_ROWS[:1]}
    announcements, _ = _hkex_read(monkeypatch, rows_of=rows.get)
    assert len(announcements) == 1


# ── NCCN 指南（nccn）────────────────────────────────────────────────────

def test_nccn_detail_keeps_the_guideline_pdfs_and_leaves_out_the_site_wide_promo_pdfs():
    """第一个 h4 之前的 PDF（化疗模板说明、年会招商册、展位图）整站每页都有，不是这份指南的文件：每份指南都下载一遍是重复下载
    （一次读取 12MB 里大半是它们）。留下各 section 下的 8 个指南 PDF。fixture 是 2026-11-12 存下来的 ALL 指南页的主要部分。"""
    from biz_rules.extractors.nccn.nccn import extract_nccn_detail

    parsed = extract_nccn_detail(page("nccn_detail.html"))
    assert parsed["title"] == "Acute Lymphoblastic Leukemia" and parsed["version"] == "3.2026"
    assert [(s["section_name"], len(s["pdfs"])) for s in parsed["sections"]] == [
        ("Guidelines", 1), ("Evidence Blocks", 1), ("Frameworks", 3), ("Guidelines for Patients", 2), ("International", 1)]
    urls = [a["url"] for a in parsed["attachment_links"]]
    assert len(urls) == 8 and not [u for u in urls if "/docs/default-source/" in u]


def test_nccn_pages_are_rendered_without_waiting_for_network_idle_or_loading_images():
    """页面挂着很多第三方脚本，等“网络空闲”一页要一两分钟：列表页等指南链接、详情页等 section 小标题出现就够了。"""
    from biz_rules.extractors.nccn import nccn_fetcher

    seen = []

    class Fetcher:
        def fetch(self, url, **options):
            seen.append((url, options))
            return "page"

    assert nccn_fetcher._render(Fetcher(), "https://www.nccn.org/x", nccn_fetcher.DETAIL_READY) == "page"
    [(url, options)] = seen
    assert options["network_idle"] is False and options["disable_resources"] is True
    assert options["wait_selector"] == "h4" and options["timeout"] == 45000


# ── Roche 新闻稿（roche）────────────────────────────────────────────────

def test_roche_list_titles_are_read_from_inside_the_nested_teaser_link():
    """列表里每条是 <h3><a><span class="teaser__title">标题<span class="teaser__icon">…</span></span></a></h3>。
    以前取 h3 元素自己的文字（.text），得到空串：入库的 Roche 新闻稿标题全是空的。"""
    from biz_rules.extractors.roche.roche_web_fetcher import _parse_list_page

    items = _parse_list_page(page("roche_teasers.html"), set(), 2026, 1)
    assert [i["source_url"] for i in items] == [
        "https://www.roche.com/media/releases/med-cor-2026-11-10",
        "https://www.roche.com/media/releases/med-cor-2026-10-28",
        "https://www.roche.com/media/releases/med-cor-2026-10-27"]
    assert items[0]["title"] == ("Roche presents new data across pharmaceuticals and diagnostics from its growing "
                                 "Alzheimer’s disease portfolio at CTAD 2026")
    assert all(i["title"] for i in items) and items[0]["publish_date"] == "2026-11-10"
