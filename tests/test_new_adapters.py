"""2026-11-17 接通的读取函数：预印本、arXiv、SEC filing、通用 RSS。都用罐头响应，不联网。"""
import httpx

from fetch import transport
from fetch.sites import arxiv, biorxiv, rss_press, sec_filings
from store.record_models import GroupStart, ItemUnreadable, make_uuid


def serve(monkeypatch, answers):
    """transport.get 按网址里的关键字给罐头响应；没有对应的回 404。"""
    def get(url, params=None, **_):
        for key, body in answers.items():
            if key in url:
                kind = {"json": body} if isinstance(body, (dict, list)) else {"content": body}
                return httpx.Response(200, request=httpx.Request("GET", url), **kind)
        return httpx.Response(404, request=httpx.Request("GET", url))
    monkeypatch.setattr(transport, "get", get)


def test_openrxiv_gives_the_latest_version_of_each_preprint_newest_first(monkeypatch):
    paper = {"doi": "10.1101/1", "title": "A  title", "abstract": "Text.", "date": "2026-11-16", "version": "1"}
    serve(monkeypatch, {"/details/biorxiv/": {"messages": [{"total": 2}], "collection": [
        paper, {**paper, "version": "2", "abstract": "Revised."}]}})
    out = list(biorxiv.iter_openrxiv({"server": "biorxiv", "days": 1}))
    assert len(out) == 1 and out[0]["uuid"] == make_uuid("biorxiv:10.1101/1")
    assert out[0]["markdown"] == "# A title\n\nRevised." and out[0]["source_url"].endswith("10.1101/1v2")


def test_arxiv_reads_the_atom_feed_and_drops_the_version_from_the_record_key(monkeypatch):
    feed = (b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>http://arxiv.org/abs/2611.00001v2</id>"
            b"<title>T</title><summary>S</summary><published>2026-11-16T01:02:03Z</published>"
            b"<category term='q-bio.BM'/></entry></feed>")
    serve(monkeypatch, {"export.arxiv.org": feed})
    out = list(arxiv.iter_arxiv({"search_query": "cat:q-bio.*"}))
    assert [(r["uuid"], r["publish_date"], r["extra"]["version"]) for r in out] == [
        (make_uuid("arxiv:2611.00001"), "2026-11-16", "2")]


def test_sec_filing_text_keeps_section_headings_and_an_oversized_filing_is_unread(monkeypatch):
    html = (b"<html><body><div><p>Item 1. | Business | 5</p></div><p>Item 1. Business</p><p>We make drugs.</p>"
            b"<table><tr><td>Revenue</td><td>10</td></tr></table></body></html>")
    assert sec_filings.html_text(html) == "Item 1. | Business | 5\n\n## Item 1. Business\n\nWe make drugs.\n\nRevenue | 10"
    recent = {"form": ["10-Q", "4", "10-K"], "filingDate": ["2026-11-03", "2026-11-02", "2026-02-20"],
              "reportDate": ["2026-09-27", "", "2025-12-31"], "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3"],
              "primaryDocument": ["q.htm", "x.xml", "k.htm"], "items": ["", "", ""]}
    serve(monkeypatch, {"company_tickers.json": {"0": {"cik_str": 78003, "ticker": "PFE", "title": "PFIZER INC"}},
                        "/submissions/": {"filings": {"recent": recent}}, "q.htm": html,
                        "k.htm": b"<p>" + b"x" * 1100000 + b"</p>"})
    out = list(sec_filings.iter_sec_filings({"site": "sec_filings", "forms": ["10-K", "10-Q"], "tickers": ["PFE"]}))
    assert out[0] == GroupStart(0) and out[1]["title"] == "PFIZER INC 10-Q（2026-09-27）"
    assert out[1]["uuid"] == make_uuid("sec_filings:0001-26-1") and "## Item 1. Business" in out[1]["markdown"]
    assert isinstance(out[2], ItemUnreadable) and "1 MiB" in out[2].reason and len(out) == 3


def test_rss_reads_rss_and_atom_items_newest_first_and_reports_a_feed_it_cannot_read(monkeypatch):
    rss = (b"<rss><channel><item><title>Old</title><link>https://a/1</link><pubDate>Wed, 11 Nov 2026 21:30:00 +0000"
           b"</pubDate><description>&lt;p&gt;Body one.&lt;/p&gt;&lt;p&gt;Two.&lt;/p&gt;</description></item></channel></rss>")
    atom = (b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>New</title><link href='https://b/2'/>"
            b"<published>2026-11-13T12:00:00Z</published><summary>Short.</summary></entry></feed>")
    serve(monkeypatch, {"a.example": rss, "b.example": atom})
    feeds = [{"name": n, "url": f"https://{n}.example/feed"} for n in ("a", "b", "c")]
    out = list(rss_press.iter_rss({"site": "rss_press", "feeds": feeds}))
    assert isinstance(out[0], ItemUnreadable) and out[0].url == "https://c.example/feed"
    assert [(r["title"], r["publish_date"], r["markdown"]) for r in out[1:]] == [
        ("New", "2026-11-13", "# New\n\nShort."), ("Old", "2026-11-11", "# Old\n\nBody one.\n\nTwo.")]
