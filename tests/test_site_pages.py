"""站点配置里的选择器和提取函数，对着真实页面的样本检查（不联网）。

tests/fixtures/site_pages/ 里的 HTML 是 2026-11-11 从真实页面存下来的，只截取选择器用到的那一块，内容没改。
站点改版后这些测试不会自己发现（样本是旧的），所以改选择器时要先看真实页面、再换样本。
"""

from pathlib import Path

import pytest
from scrapling.parser import Selector

from fetch import rate
from fetch.site_profiles import SITE_PROFILES

PAGES = Path(__file__).parent / "fixtures" / "site_pages"


@pytest.fixture(autouse=True)
def _no_rate_wait(monkeypatch):
    """打开页面之前的限速等待（fetch.transport.wait）不是这里要查的。"""
    monkeypatch.setattr(rate, "wait", lambda *a, **k: None)


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
    from fetch.extractors.cde import extract_cde_detail

    html = ("<html><head><meta name='ArticleTitle' content='某通告'><meta name='PubDate' content='20260403'></head><body>"
            "<div class='new_detail_content'><p>正文一段。</p></div>"
            "<div class='relatedNews'><a href='/main/att/download/x1'>附件一.pdf</a>"
            # the same block lists related news pages: other pages, not files (the old crawler stored 33 of them)
            "<a href='/main/news/viewInfoCommon/12842c9b5ced9beeebcca67100a11610'>相关新闻一</a>"
            "<a href='/main/newspic/view/032ea88269c937aa6eff394d46f82af4'>图片新闻</a></div></body></html>")
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
    assert profile["use_simple_fetch"] is True  # 静态 HTML，简单请求读，不开浏览器


def test_fda_press_body_is_the_release_itself_not_the_page_header():
    """详情页（2026-11-14 存的 <main>）：正文取 div[role=main]（原采集层 11-13 修正），从“For Immediate Release”起；
    原来落到 main article，开头多出“FDA News Release / 标题 / More Press Announcements”。"""
    got = SITE_PROFILES["fda_press"]["detail_extractor"](page("fda_press_detail.html"))
    assert got["title"].startswith("FDA Approves First Factor") and got["publish_date"] == "2026-11-09"
    assert got["body_text"].startswith("For Immediate Release:") and "More Press Announcements" not in got["body_text"]


# ── FDA 药品安全通讯（fda_drug_safety）───────────────────────────────────

def test_fda_drug_safety_list_is_one_static_table_read_with_a_plain_request_in_a_single_page():
    """整张表（日期 | 标题，共约 44 条）都在静态 HTML 里（浏览器里才被分成每页 10 行），标题是详情页链接，最新的在前：
    用简单请求读一页就够了，不开浏览器（浏览器开一个页面要 20-60 秒）。fixture 是 2026-11-12 存下来的整张表。"""
    from fetch.pagination import make_driver

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
    from fetch.extractors.fda.drug_safety import extract_fda_drug_safety

    for name, issued, footer in [("subtitle_date", "2026-11-09", None), ("panels", "2026-03-31", "04/03/2026"),
                                 ("plain", "2022-06-01", "06/07/2022")]:
        sample = page(f"fda_drug_safety_detail_{name}.html")
        parsed = extract_fda_drug_safety(sample)
        assert parsed["publish_date"] == issued and parsed["title"]
        if footer:  # 样本里带着这个误导的页脚日期
            assert sample.css("time")[0].get_all_text(strip=True) == footer


def test_fda_drug_safety_body_is_the_whole_article_column_also_on_pages_without_accordion_panels():
    """文章在 div[role=main] 里。老页面没有折叠面板（div.panel-group），旧选择器匹配到空元素，入库的正文是空的。"""
    from fetch.extractors.fda.drug_safety import extract_fda_drug_safety

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
    {"FILE_INFO": "1KB", "NEWS_ID": "12299999", "SHORT_TEXT": "公告及通告 - [海外監管公告-其他]<br/>", "DOD_WEB_PATH": "",
     "STOCK_NAME": "中國生物製藥", "FILE_TYPE": "HTM", "DATE_TIME": "30/09/2026 09:00",
     "TITLE": "發行人剛在本網站英文版面發布一項公告，相應中文版本或會／或不會在本版面發布。",
     "STOCK_CODE": "01177", "FILE_LINK": "/listedco/listconews/sehk/2026/0930/2026093001075_c.htm"},
]
HKEX_COMPANIES = [{"code": "00013", "name": "和黃醫藥", "stock_id": 1000001102},
                  {"code": "01177", "name": "中國生物製藥", "stock_id": 6833}]


def _hkex_stub(monkeypatch, overrides=None, rows_of=None):
    """用替身接口顶掉 hkex 用的 transport.get，返回 (站点接口配置, 发出的请求参数)。rows_of(stock_id) 给这家公司的行；
    默认 00013 给前两行、01177 给第三行；返回异常对象就是这家读不到。"""
    import json

    from fetch.extractors import hkex_fetcher

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

    monkeypatch.setattr(hkex_fetcher.transport, "get", get)
    return {**SITE_PROFILES["hkex"]["api"], "companies": HKEX_COMPANIES, **(overrides or {})}, sent


def _hkex_read(monkeypatch, overrides=None, rows_of=None, start=0):
    """用替身接口读一遍 hkex 的拉取函数，返回 (读到的公告，不含每家公司开头的 GroupStart, 发出的请求参数)。"""
    from fetch.extractors import hkex_fetcher
    from store.record_models import GroupStart

    api, sent = _hkex_stub(monkeypatch, overrides, rows_of)
    items = list(hkex_fetcher.fetch_hkex_announcements(api, start))
    return [item for item in items if not isinstance(item, GroupStart)], sent


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
    """“展示文件”行只是提示语，不是公告：不读。同一句提示语也会出现在普通文件类型下（链接到一页只有这句话的 .htm，
    14 家公司的 2187 条里有 35 条），按标题也跳过。普通公告照常读到。"""
    announcements, _ = _hkex_read(monkeypatch)
    assert [a["stock_code"] for a in announcements] == ["00013", "01177"]
    assert announcements[0]["pdf_url"].endswith("/2026/1105/2026110500118_c.pdf")
    kept, _ = _hkex_read(monkeypatch, {"skip_headlines": [], "skip_titles": []})
    assert len(kept) == 4


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
    from fetch.api_fields import to_result

    first = _hkex_read(monkeypatch)[0][0]
    parsed, kept = to_result("hkex", SITE_PROFILES["hkex"], first)
    assert parsed["title"] == first["title"] and parsed["publish_date"] == "2026-11-05"
    assert parsed["attachment_links"] == [{"url": first["pdf_url"], "name": first["title"]}]
    assert kept is first


def test_hkex_company_that_cannot_be_read_is_given_as_unreadable_with_its_name_and_the_next_one_is_still_read(monkeypatch):
    """一家读不到（网络、状态码）：给一个 ItemUnreadable，写明是哪家、为什么，采集插件记下它、不存版本；
    接着读下一家，不是整个来源停在这里。"""
    from store.record_models import ItemUnreadable

    def rows_of(stock_id):
        return TimeoutError("read timed out") if stock_id == 1000001102 else HKEX_ROWS[2:]

    unreadable, announcement = _hkex_read(monkeypatch, rows_of=rows_of)[0]
    assert isinstance(unreadable, ItemUnreadable) and "stockId=1000001102" in unreadable.url
    assert "和黃醫藥" in unreadable.reason and "00013" in unreadable.reason and "TimeoutError" in unreadable.reason
    assert announcement["stock_code"] == "01177"


def test_hkex_a_company_is_a_group_and_a_read_can_start_at_any_company_without_asking_for_the_ones_before(monkeypatch):
    """每家公司开头给一个 GroupStart(第几家)，先于这家公司的请求（请求时间到了，续读位置就是这一家）；
    从第 start 家读起，前面的公司不发请求。"""
    from fetch.extractors import hkex_fetcher
    from store.record_models import GroupStart

    api, sent = _hkex_stub(monkeypatch)
    items = list(hkex_fetcher.fetch_hkex_announcements(api))
    assert [i for i in items if isinstance(i, GroupStart)] == [GroupStart(0), GroupStart(1)]
    assert items.index(GroupStart(1)) == 2 and len(sent) == 2  # 第 0 组：GroupStart + 一条公告（展示文件跳过），第 1 组在后
    api, sent = _hkex_stub(monkeypatch)
    rest = list(hkex_fetcher.fetch_hkex_announcements(api, 1))
    assert [i for i in rest if isinstance(i, GroupStart)] == [GroupStart(1)] and len(rest) == 2
    assert rest[1]["stock_code"] == "01177" and len(sent) == 1 and sent[0]["stockId"] == 6833


def test_hkex_the_rmb_counter_of_a_dual_counter_stock_is_not_read_again_but_a_lone_8xxxx_code_is(monkeypatch):
    """86618 (RMB counter) lists the same documents as 06618 (HKD counter). Reading both let the version of each of those
    announcements change at every visit, because the stock code we store differs; only the HKD counter is read."""
    companies = [{"code": "06618", "name": "京東健康", "stock_id": 1}, {"code": "86618", "name": "京東健康－人民幣", "stock_id": 2},
                 {"code": "80737", "name": "沒有港元柜台的", "stock_id": 3}]
    rows = {1: HKEX_ROWS[:1], 2: HKEX_ROWS[:1], 3: HKEX_ROWS[2:3]}
    announcements, sent = _hkex_read(monkeypatch, {"companies": companies}, rows_of=rows.get)
    assert [params["stockId"] for params in sent] == [1, 3]  # 2 is the twin of 1: no request
    assert [a["stock_code"] for a in announcements] == ["06618", "80737"]


def test_hkex_a_joint_announcement_under_two_companies_is_given_once(monkeypatch):
    """联合公告会出现在几家公司名下，指向同一个 PDF：同一个文件只给一次，不然同一条记录一次读两遍。"""
    rows = {1000001102: HKEX_ROWS[:1], 6833: HKEX_ROWS[:1]}
    announcements, _ = _hkex_read(monkeypatch, rows_of=rows.get)
    assert len(announcements) == 1


# ── NCCN 指南（nccn）────────────────────────────────────────────────────

def test_nccn_detail_attaches_only_the_pdfs_that_can_be_downloaded_and_leaves_out_the_site_wide_promo_pdfs():
    """第一个 h4 之前的 PDF（化疗模板说明、年会招商册、展位图）整站每页都有，不是这份指南的文件：每份指南都下载一遍是重复下载
    （一次读取 12MB 里大半是它们）。医生版指南 PDF（/professionals/…）要登录，下载到的是登录页，每次内容不同，读一次多一个
    “版本”：不当附件，链接仍留在 sections 里。公开的患者版 PDF 当附件。fixture 是 2026-11-12 存下来的 ALL 指南页的主要部分。"""
    from fetch.extractors.nccn.nccn import extract_nccn_detail

    parsed = extract_nccn_detail(page("nccn_detail.html"))
    assert parsed["title"] == "Acute Lymphoblastic Leukemia" and parsed["version"] == "3.2026"
    assert [(s["section_name"], len(s["pdfs"])) for s in parsed["sections"]] == [
        ("Guidelines", 1), ("Evidence Blocks", 1), ("Frameworks", 3), ("Guidelines for Patients", 2), ("International", 1)]
    assert [a["url"].rsplit("/", 1)[1] for a in parsed["attachment_links"]] == ["all-patient.pdf", "ALL-es-patient.pdf"]


def test_nccn_a_guideline_is_a_group_and_a_read_starts_at_any_group_without_rendering_what_is_before_it(monkeypatch):
    """组号 = 分类编号 × 1000 + 它在分类列表里的序号。从组号 start 读起：前面的指南详情页不渲染，整个在 start 之前的分类连列表页也
    不渲染（续读每段只渲染一页列表）。"""
    import scrapling.fetchers

    from fetch.extractors.nccn import nccn_fetcher
    from store.record_models import GroupStart

    by_category = {"1": ["a0", "a1", "a2"], "2": ["b0", "b1"]}
    rendered = []

    class Fetcher:
        def __init__(self, **kwargs):
            pass

        def fetch(self, url, **kwargs):
            rendered.append(url)
            return url

    def parse(page, cat_num, cat_name):
        return [{"title": g, "version": "1.2026", "detail_url": f"https://www.nccn.org/{g}", "guideline_id": g,
                 "category_name": cat_name} for g in by_category[cat_num]]

    monkeypatch.setattr(scrapling.fetchers, "StealthyFetcher", Fetcher)
    monkeypatch.setattr(nccn_fetcher, "_parse_rendered_list", parse)
    monkeypatch.setattr(nccn_fetcher, "extract_nccn_detail",
                        lambda page: {"title": "t", "version": "1.2026", "attachment_links": [], "sections": []})
    api = {"categories": ["Treatment by Cancer Type", "Detection, Prevention, and Risk Reduction"]}

    def read(start):
        rendered.clear()
        items = list(nccn_fetcher.iter_nccn(api, start))
        groups = [i.index for i in items if isinstance(i, GroupStart)]
        records = [i["guideline_id"] for i in items if isinstance(i, dict)]
        lists = [u.rsplit("_", 1)[1] for u in rendered if "category_" in u]
        details = [u.rsplit("/", 1)[1] for u in rendered if "category_" not in u]  # 渲染过的详情页
        return groups, records, lists, details

    assert read(0) == ([1000, 1001, 1002, 2000, 2001], ["a0", "a1", "a2", "b0", "b1"], ["1", "2"], ["a0", "a1", "a2", "b0", "b1"])
    assert read(1002) == ([1002, 2000, 2001], ["a2", "b0", "b1"], ["1", "2"], ["a2", "b0", "b1"])  # a0、a1 的详情页没有渲染
    assert read(2001) == ([2001], ["b1"], ["2"], ["b1"])  # 第 1 类整个在前面：连列表页也没有渲染


def test_nccn_pages_are_rendered_without_waiting_for_network_idle_or_loading_images():
    """页面挂着很多第三方脚本，等“网络空闲”一页要一两分钟：列表页等指南链接、详情页等 section 小标题出现就够了。"""
    from fetch.extractors.nccn import nccn_fetcher

    seen = []

    class Fetcher:
        def fetch(self, url, **options):
            seen.append((url, options))
            return "page"

    assert nccn_fetcher._render(Fetcher(), "https://www.nccn.org/x", nccn_fetcher.DETAIL_READY) == "page"
    [(url, options)] = seen
    assert options["network_idle"] is False and options["disable_resources"] is True
    assert options["wait_selector"] == "h4" and options["timeout"] == 20000 and options["retries"] == 1


def test_nccn_reads_through_its_own_proxy_only_when_the_variable_is_set(monkeypatch):
    """DATAFABRIC_PROXY_NCCN 设了：nccn 的渲染经它（附件下载用同一个 site_proxy）；没设：直连，和以前一样；别的站点不受影响。"""
    from fetch import site_crawler
    from fetch.extractors.nccn import nccn_fetcher

    seen = []

    class Fetcher:
        def fetch(self, url, **options):
            seen.append(options.get("proxy"))
            return "page"

    monkeypatch.delenv("DATAFABRIC_PROXY_NCCN", raising=False)
    nccn_fetcher._render(Fetcher(), "https://www.nccn.org/x", "h4", site_crawler.site_proxy("nccn"))
    monkeypatch.setenv("DATAFABRIC_PROXY_NCCN", "http://127.0.0.1:7890")
    nccn_fetcher._render(Fetcher(), "https://www.nccn.org/x", "h4", site_crawler.site_proxy("nccn"))
    assert seen == [None, "http://127.0.0.1:7890"] and site_crawler.site_proxy("nmpa") is None


def test_nccn_a_render_that_hangs_is_tried_again_in_a_new_browser_and_the_last_failure_is_the_one_raised():
    """卡在坏地址上的那次渲染 20 秒放弃，重来；每次重来都是新的 fetch 调用（新浏览器，换一次域名解析）。"""
    from fetch.extractors.nccn import nccn_fetcher

    calls = []

    class Fetcher:
        def __init__(self, hangs):
            self.hangs = hangs

        def fetch(self, url, **options):
            calls.append(url)
            if len(calls) <= self.hangs:
                raise TimeoutError(f"net::ERR_TIMED_OUT {len(calls)}")
            return "page"

    assert nccn_fetcher._render(Fetcher(hangs=3), "https://www.nccn.org/x", "h4") == "page" and len(calls) == 4
    calls.clear()
    try:
        nccn_fetcher._render(Fetcher(hangs=99), "https://www.nccn.org/x", "h4")
    except TimeoutError as exc:
        assert str(exc).endswith(str(nccn_fetcher.RENDER_TRIES)) and len(calls) == nccn_fetcher.RENDER_TRIES
    else:
        raise AssertionError("the last failure should be raised")


# ── Roche 新闻稿（roche）────────────────────────────────────────────────

def test_roche_list_titles_are_read_from_inside_the_nested_teaser_link():
    """列表里每条是 <h3><a><span class="teaser__title">标题<span class="teaser__icon">…</span></span></a></h3>。
    以前取 h3 元素自己的文字（.text），得到空串：入库的 Roche 新闻稿标题全是空的。"""
    from fetch.extractors.roche.roche_web_fetcher import _parse_list_page

    items = _parse_list_page(page("roche_teasers.html"), set(), 2026, 1)
    assert [i["source_url"] for i in items] == [
        "https://www.roche.com/media/releases/med-cor-2026-11-10",
        "https://www.roche.com/media/releases/med-cor-2026-10-28",
        "https://www.roche.com/media/releases/med-cor-2026-10-27"]
    assert items[0]["title"] == ("Roche presents new data across pharmaceuticals and diagnostics from its growing "
                                 "Alzheimer’s disease portfolio at CTAD 2026")
    assert all(i["title"] for i in items) and items[0]["publish_date"] == "2026-11-10"


# ── ChiCTR 登记详情页（chictr）───────────────────────────────────────────

def test_chictr_upload_link_is_the_only_attachment_and_is_named_by_its_field():
    """chictr_upload.html 是 2026-11-12 的真实页面（317987）：标题、注册号一行、伦理批件一行（“查看附件”没有链接）、
    “试验完成后的统计结果（上传文件）”一栏（中文行和英文行各有一个同一令牌的 DownloadTest 链接）。只有统计结果是附件：
    只取一个，用字段名作 key（令牌每次都变，不能当身份）；页面顶上的 DownloadXml 是记录本身的 XML，不算。
    path 为空 = 没有文件，不算附件。"""
    import re

    html = (PAGES / "chictr_upload.html").read_text(encoding="utf-8")
    extract = SITE_PROFILES["chictr"]["detail_extractor"]
    got = extract(Selector(html))
    href = re.search(r'href="\./(bin/chictr/DownloadTest\?path=[^"]+)"', html).group(1)
    assert got["attachment_links"] == [{"url": "https://www.chictr.org.cn/" + href,
                                        "key": "试验完成后的统计结果（上传文件）"}]
    assert got["title"].startswith("基于叙事教学法的死亡教育项目") and got["extra"]["regno"] == "ChiCTR2600136516"
    no_file = html.replace(href, "bin/chictr/DownloadTest?path=")
    assert extract(Selector(no_file))["attachment_links"] == []


# ── NMPA / PhIRDA（2026-11-20 从库里存的原页面截取：nmpa_detail、phirda_detail、phirda_list）──────────────

def test_nmpa_body_is_the_document_not_the_info_table_share_bar_or_related_links():
    from fetch.extractors.nmpa import extract_nmpa_detail

    got = extract_nmpa_detail(page("nmpa_detail.html"))
    body = got["body_text"]
    assert got["title"] == "国家药监局关于发布医疗器械出口销售证明管理规定的公告（2025年第126号）"
    assert got["publish_date"] == "2025-12-25"
    assert body.startswith("为支持医疗器械出口贸易")
    assert "索引号" not in body and "发布时间" not in body and "相关链接" not in body and "分享" not in body
    # 段落之间空一行；只是一个附件链接的那一行不留在正文里（附件另有记录）
    assert body.endswith("附件：医疗器械出口销售证明管理规定\n\n国家药监局\n\n2025年12月24日")
    # 不再从正文里猜药名、企业、批准文号；文号从标题里取
    assert got["extra"] == {"index_number": "FGWJ-2025-10097", "doc_number": "2025年第126号"}
    assert got["attachment_links"] == [{"name": "国家药品监督管理局2025年第126号公告附件.doc",
                                        "url": "https://www.nmpa.gov.cn/directory/web/nmpa/images/1766628495442076887.doc"}]


def _nmpa(text_html, after=""):
    return Selector("<html><head><meta name='ArticleTitle' content='某通知'></head><body><div class='wenzhang'>"
                    "<h2 class='title'>某通知</h2><div class='text'>" + text_html + "</div>" + after
                    + "</div></body></html>")


def test_nmpa_short_numbered_lines_are_headings_and_paragraphs_are_kept_apart():
    from fetch.extractors.nmpa import extract_nmpa_detail

    body = extract_nmpa_detail(_nmpa(
        "<p>一、背景</p><p>正文一段。</p><p>（一）申请人企业法人资格证明文件。</p><p>（二）工作要求</p>"
        "<p>1.产品的预期用途</p><p>一、各级药品监督管理部门应当按照本通知要求，认真组织实施，确保各项工作落到实处。</p>"
        "<p>第一章 总则</p>"))["body_text"]
    assert body.split("\n\n") == ["## 一、背景", "正文一段。", "（一）申请人企业法人资格证明文件。", "### （二）工作要求",
                                  "#### 1.产品的预期用途",
                                  "一、各级药品监督管理部门应当按照本通知要求，认真组织实施，确保各项工作落到实处。",
                                  "## 第一章 总则"]


def test_nmpa_related_links_hidden_and_textless_anchors_are_not_kept():
    from fetch.extractors.nmpa import extract_nmpa_detail

    got = extract_nmpa_detail(_nmpa(
        "<p>正文。</p><p><a href='/directory/web/nmpa/images/a.docx'>附件1.docx</a></p>"
        "<p><a href='/directory/web/nmpa/images/b.doc'></a></p>"
        "<p style='display:none'><a href='/directory/web/nmpa/images/c.doc'>附件1.doc</a></p>"
        "<p>【相关链接】</p><p><a href='../x/1.html'>另一篇</a></p>",
        "<div class='about'><div class='title'>【相关链接】</div><ul><li><a href='y.html'>又一篇</a></li></ul></div>"))
    assert got["body_text"] == "正文。"
    assert got["attachment_links"] == [{"name": "附件1.docx", "url": "https://www.nmpa.gov.cn/directory/web/nmpa/images/a.docx"}]


def test_nmpa_image_and_video_pages_say_the_content_is_in_the_media():
    from fetch.extractors.nmpa import extract_nmpa_detail

    video = extract_nmpa_detail(_nmpa("<p><video src='/directory/web/nmpa/video/1.mp4'><source src='/directory/web/"
                                      "nmpa/video/1.mp4'></video></p>"))
    assert video["body_text"] == "" and video["page_has_no_text"]
    assert video["extra"] == {"media": {"videos": ["https://www.nmpa.gov.cn/directory/web/nmpa/video/1.mp4"]},
                              "content_in_media": "video"}
    poster = extract_nmpa_detail(_nmpa("<div class='special-swipe'><p><img src='/directory/web/nmpa/images/1.png'>"
                                       "</p></div><p><br></p>"))
    assert poster["extra"]["content_in_media"] == "image"
    assert poster["extra"]["media"] == {"images": ["https://www.nmpa.gov.cn/directory/web/nmpa/images/1.png"]}


def test_nmpa_sub_pages_of_a_multi_page_article_are_found_in_page_order():
    from fetch.extractors.nmpa import continuation_links, extract_nmpa_continuation

    pager = ("<div class='page'><div class='gray'><a href='20140207154501788.html'>首页</a><span>1</span>"
             "<a href='20140207154501788_1.html'>2</a><a href='20140207154501788_2.html'>3</a>"
             "<a href='20140207154501788_1.html'>下一页</a><span>共3页</span></div></div>")
    first = _nmpa("<p>第五条 甲。</p>", pager)
    assert continuation_links(first) == ["20140207154501788_1.html", "20140207154501788_2.html"]
    assert continuation_links(_nmpa("<p>单页。</p>")) == []
    sub = extract_nmpa_continuation(_nmpa("<p>第六条 乙。</p>", pager))
    assert sub["body_text"] == "第六条 乙。"  # 标题、状态标记、翻页条都不重复


def test_nmpa_long_table_is_split_into_groups_that_each_repeat_the_header():
    from fetch.extractors.nmpa import extract_nmpa_detail

    rows = "".join("<tr><td>%d</td><td>%s</td></tr>" % (i, "药" * 400) for i in range(12))
    body = extract_nmpa_detail(_nmpa("<table><tr><td>序号</td><td>名称</td></tr>" + rows + "</table>"))["body_text"]
    groups = body.split("\n\n")
    assert len(groups) > 1 and all(g.startswith("| 序号 | 名称 |\n| --- | --- |\n") for g in groups)
    assert all(len(g) <= 3000 for g in groups)
    assert sum(g.count("\n| ") - 1 for g in groups) == 12  # 每行都在，且只在一组里


def test_phirda_list_takes_the_column_items_not_the_hot_news_sidebar():
    for site in ("phirda_industry", "phirda_policy", "phirda_rd", "phirda_healthcare_reform"):
        links = page("phirda_list.html").css(SITE_PROFILES[site]["list_item_selector"])
        hrefs = [a.attrib["href"] for a in links]
        assert len(hrefs) == len(set(hrefs)) == 8  # 左栏 8 篇，每篇一个标题链接
        assert "/artilce_43268.html?module=trackingCodeGenerator" in hrefs
        assert not any("artilce_44033" in h for h in hrefs)  # 右栏“热门资讯”的第一篇


def test_phirda_body_keeps_a_sentence_on_one_line_across_bold_and_links():
    from fetch.extractors.phirda import extract_phirda_detail

    body = extract_phirda_detail(page("phirda_detail.html"))["body_text"]
    assert "云端按语：脉流科技" in body.replace(" ", "")
    assert not any(line.startswith("：") for line in body.split("\n"))


def test_block_text_breaks_lines_at_blocks_only():
    from fetch.extractors.text_tools import block_text

    html = "<div><p>甲<b>乙</b><a href='#'>丙</a>。</p><p>第二段<br>换行</p><script>x()</script><!-- 注释 -->尾</div>"
    assert block_text(html) == "甲乙丙。\n第二段\n换行\n尾"


def test_chictr_placeholder_registration_date_falls_back_to_last_update():
    from fetch.extractors.chictr.chictr import extract_chictr_detail

    rows = ("<tr><td><p class='cn'>注册时间：</p></td><td>1990-01-01</td></tr>"
            "<tr><td><p class='cn'>最近更新日期：</p></td><td>2016-02-24</td></tr>"
            "<tr><td><p class='cn'>注册号：</p></td><td>ChiCTR-IIR-16007986</td></tr>")
    got = extract_chictr_detail(Selector(f"<html><body><table>{rows}</table></body></html>"))
    assert got["publish_date"] == "2016-02-24"


def test_nmpa_old_layout_table_is_a_compact_markdown_table_and_an_image_page_has_no_text():
    from fetch.extractors.nmpa import extract_nmpa_detail
    from fetch.page_parser import route_webpage

    table = ("<html><head><meta name='ArticleTitle' content='飞行检查通报'></head><body><div class='wenzhang'>"
             "<div class='text'><table><tr><td>企业名称</td><td>甲公司</td></tr>"
             "<tr><td>检查发现</td><td>一大段文字 | 含竖线</td></tr></table></div></div></body></html>")
    assert extract_nmpa_detail(Selector(table))["body_text"].splitlines() == [
        "| 企业名称 | 甲公司 |", "| --- | --- |", "| 检查发现 | 一大段文字 ｜ 含竖线 |"]
    images = ("<html><head><meta name='ArticleTitle' content='图解政策'></head><body><div>中 En 无障碍</div>"
              "<div class='wenzhang'><div class='date'>发布时间：2017-09-06</div><div class='text'>"
              "<p><img src='/1.jpg'></p></div></div></body></html>")
    parsed = route_webpage({"url": "https://www.nmpa.gov.cn/x.html", "page": Selector(images), "uuid": "u"}, "nmpa")
    assert parsed["parse_method"] == "css_extract" and parsed["markdown"] == "" and parsed["title"] == "图解政策"
