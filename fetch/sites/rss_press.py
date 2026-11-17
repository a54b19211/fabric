"""通用 RSS / Atom 读取：企业新闻稿的订阅源。要加一个源，在下面 FEEDS 里加一行。EMA 新闻见 fetch/sites/ema.py。

只用订阅源自己发布的内容（标题、链接、日期、正文或摘要），不再去抓文章页。几个源并成一个从新到旧的队列。
一条 = 一个链接。回 403 / 人机验证的源不绕过（2026-11-17 实测 Lilly、Amgen、Regeneron、AstraZeneca、BMS、
GlobeNewswire、Business Wire 的订阅地址都被拦），所以没列。
"""
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from fetch import transport
from fetch.api_fields import record
from store.record_models import ItemUnreadable

SITE_KEY = "rss_press"
FEEDS = [
    {"name": "Pfizer", "url": "https://www.pfizer.com/newsfeed"},  # 正文全文
    # 只有摘要（两三句）；全文在 prnewswire.com 文章页，不抓
    {"name": "PR Newswire Pharmaceuticals", "url": "https://www.prnewswire.com/rss/health-latest-news/pharmaceuticals-list.rss"},
]
_NS = {"content": "{http://purl.org/rss/1.0/modules/content/}encoded", "atom": "{http://www.w3.org/2005/Atom}"}


def _plain(html) -> str:
    """订阅源里的 HTML 片段 → 一块一段的文字。"""
    text = BeautifulSoup(html or "", "lxml").get_text("\n")
    return "\n\n".join(" ".join(line.split()) for line in text.split("\n") if line.strip())


def _day(value) -> str:
    """RSS 的 RFC 822 日期或 Atom 的 ISO 日期 → YYYY-MM-DD；认不出来就是空。"""
    value = (value or "").strip()
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return value[:10] if value[:4].isdigit() else ""


def _items(feed, xml):
    """一个订阅源的条目 [(日期, 标题, 链接, 正文)]；RSS 2.0 的 item 和 Atom 的 entry 都认。"""
    root, atom, out = ET.fromstring(xml), _NS["atom"], []
    for item in root.iter("item"):
        body = item.findtext(_NS["content"]) or item.findtext("description")
        out.append((_day(item.findtext("pubDate")), item.findtext("title"), item.findtext("link"), body))
    for entry in root.iter(atom + "entry"):
        link = entry.find(atom + "link")
        body = entry.findtext(atom + "content") or entry.findtext(atom + "summary")
        out.append((_day(entry.findtext(atom + "published") or entry.findtext(atom + "updated")),
                    entry.findtext(atom + "title"), link.get("href") if link is not None else None, body))
    return [(day, " ".join((title or "").split()), (link or "").strip(), _plain(body), feed["name"])
            for day, title, link, body in out if (title or "").strip() and (link or "").strip()]


def iter_rss(api):
    items = []
    for feed in api["feeds"]:
        try:
            response = transport.get(feed["url"], timeout=30, cap=16 * 1024 * 1024, headers={
                "User-Agent": "DataFabric-Research (academic, non-commercial)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
            response.raise_for_status()
            items += _items(feed, response.content)
        except Exception as exc:  # noqa: BLE001 -- 一个源读不到不拦着别的源，但要报出来
            yield ItemUnreadable(feed["url"], f"订阅源读取失败：{type(exc).__name__}")
    for day, title, link, body, name in sorted(items, key=lambda item: item[0], reverse=True):
        yield record(api["site"], link, title, day, f"# {title}\n\n{body}" if body else f"# {title}", link,
                     category=api.get("category", "企业新闻稿"), summary=body, extra={"feed": name})


CONFIG = {
    "name": "企业新闻稿（RSS）",
    "source_type": "api",
    "url_pattern": "rss",
    "api": {"site": SITE_KEY, "feeds": FEEDS},
    "api_fetcher": iter_rss,
}
