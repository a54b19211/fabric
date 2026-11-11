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
