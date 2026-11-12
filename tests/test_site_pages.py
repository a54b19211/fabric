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

def test_fda_drug_safety_list_is_a_table_paged_by_its_next_button():
    """列表是一张表（日期 | 标题，10 行一页，共约 44 条），标题是详情页链接；翻页只能点表下面的“下一页”
    （地址上没有页码，?page=N 返回的还是第一页）。第一页时“上一页”是 disabled，“下一页”可点。"""
    profile = SITE_PROFILES["fda_drug_safety"]
    sample = page("fda_drug_safety_list.html")
    links = sample.css(profile["list_item_selector"])
    assert len(links) == 10 and sample.css(profile["wait_selector"])
    assert all(a.attrib["href"].startswith("/drugs/") for a in links)
    assert profile["pagination"]["type"] == "js_click"
    assert len(sample.css(profile["pagination"]["next_btn_selector"])) == 1
    assert not sample.css("li.paginate_button.previous:not(.disabled)")


# ── 港交所披露易（hkex）──────────────────────────────────────────────────

# 接口返回的真实两行（和黄医药 00013）：一行是普通公告，一行是“展示文件”（只是一句提示语，各公司指向同一个网址）
HKEX_ROWS = """<table class="table"><tbody>
<tr><td class="release-time"><span class="mobile-list-heading">發放時間: </span>05/11/2026 08:00</td>
<td class="stock-short-code"><span class="mobile-list-heading">股份代號: </span>00013</td>
<td class="stock-short-name"><span class="mobile-list-heading">股份簡稱: </span>和黃醫藥</td>
<td><span class="mobile-list-heading">文件: </span><div class="headline">公告及通告 - [海外監管公告-業務發展最新情況]<br/></div>
<div class="doc-link"><a href="/listedco/listconews/sehk/2026/1105/2026110500118_c.pdf">自願性公告 - 和黃醫藥宣佈索樂匹尼布獲FDA孤兒藥資格認定</a> (<span class="attachment_filesize">200KB</span>)</div></td></tr>
<tr><td class="release-time"><span class="mobile-list-heading">發放時間: </span>09/04/2026 16:44</td>
<td class="stock-short-code"><span class="mobile-list-heading">股份代號: </span>00013</td>
<td class="stock-short-name"><span class="mobile-list-heading">股份簡稱: </span>和黃醫藥</td>
<td><span class="mobile-list-heading">文件: </span><div class="headline">展示文件<br/></div>
<div class="doc-link"><a href="/listedco/listconews/sehk/dod/tc.html">發行人剛在本網站英文版面發布一項公告，相應中文版本或會／或不會在本版面發布。</a> (<span class="attachment_filesize">1KB</span>, HTM)</div></td></tr>
</tbody></table>"""


def _hkex_read(monkeypatch, overrides=None):
    """用替身接口读一遍 hkex 的拉取函数，返回 (读到的公告, 发出的表单参数)。"""
    from biz_rules.extractors import hkex_fetcher

    sent = []

    class Reply:
        text = HKEX_ROWS

        def raise_for_status(self):
            pass

    monkeypatch.setattr(hkex_fetcher.requests, "post", lambda url, data, **kw: (sent.append(data), Reply())[1])
    api = {**SITE_PROFILES["hkex"]["api"], "companies": [{"code": "00013", "name": "和黃醫藥", "stock_id": 1000001102}]}
    api.update(overrides or {})
    return list(hkex_fetcher.fetch_hkex_announcements(api)), sent


def test_hkex_end_date_defaults_to_today_and_is_sent_as_yyyymmdd(monkeypatch):
    """截止日写死成 20260410 时，之后发布的公告全读不到。留空 = 读取当天；接口不接受空的 to（返回 0 条），所以不能原样传空。"""
    from datetime import date

    assert SITE_PROFILES["hkex"]["api"]["to_date"] == ""
    _, sent = _hkex_read(monkeypatch)
    assert sent[0]["to"] == date.today().strftime("%Y%m%d") and sent[0]["from"] == "20240101"
    _, sent = _hkex_read(monkeypatch, {"to_date": "20261231"})
    assert sent[0]["to"] == "20261231"


def test_hkex_skips_the_display_document_notice_rows(monkeypatch):
    """“展示文件”行只是提示语，不是公告：不读。普通公告照常读到。"""
    announcements, _ = _hkex_read(monkeypatch)
    assert [a["headline"] for a in announcements] == ["公告及通告 - [海外監管公告-業務發展最新情況]"]
    assert announcements[0]["pdf_url"].endswith("/2026/1105/2026110500118_c.pdf")
    kept, _ = _hkex_read(monkeypatch, {"skip_headlines": []})
    assert len(kept) == 2


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
