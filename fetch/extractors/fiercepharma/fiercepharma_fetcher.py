"""
Fierce Pharma RSS + 详情页混合采集器。

流程：
  1. 用 fetch.transport.get 拉取 RSS XML，解析出文章 URL 和基本元数据
  2. 对每篇文章用 StealthyFetcher 渲染详情页，提取标题和正文（RSS 里的标题是空的，只有详情页有）
  3. yield 标准记录 dict（与 route_webpage 输出格式一致）
详情页没读全（403、超时、标题或正文缺失）：重读一次，还不行就记为读不到（ItemUnreadable），这一篇不存版本，接着读下一篇。
"""

import re
import time
from datetime import datetime
from typing import Iterator
from xml.etree import ElementTree as ET

from loguru import logger
from scrapling.fetchers import StealthyFetcher

from fetch import transport
from fetch.extractors.fiercepharma.fiercepharma import extract_fiercepharma_detail
from store.history_models import ItemUnreadable, make_uuid

# RSS 命名空间
_DC_NS = "http://purl.org/dc/elements/1.1/"

# 和 fetch/sites/fiercepharma.py 的 rss_feeds 一样。总源 https://www.fiercepharma.com/rss/xml 已去掉：
# 它每次回 403（Cloudflare 的人机验证，响应头 cf-mitigated: challenge；/rss 也一样），没有替代地址
# （/rss.xml 只有 10 条，其中 9 条是网络研讨会、资源页，不是新闻），不绕过验证。读不到它之前也没给过一条，覆盖没变。
RSS_FEEDS = [
    "https://www.fiercepharma.com/rss/Pharma/xml",
    "https://www.fiercepharma.com/rss/manufacturing/xml",
]

RETRY_DELAY = 10.0  # 第一次没读全，隔多久重读一次（秒）
MIN_BODY_CHARS = 1000  # 正文比这短，当作页面没读全（整篇文章实测五千字上下）


def _parse_rss(xml_bytes: bytes) -> list[dict]:
    """解析 RSS XML，返回文章基本信息列表。"""
    items = []
    channel = ET.fromstring(xml_bytes).find("channel")  # 解析不了就抛 ParseError：由调用方记成这个 feed 读不到
    if channel is None:
        raise ValueError("RSS 里没有 channel")

    for item in channel.findall("item"):
        link_el = item.find("link")
        title_el = item.find("title")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")
        guid_el = item.find("guid")

        link = (link_el.text or "").strip() if link_el is not None else ""
        # guid 有时是真实 URL，有时是 UUID 形式
        guid = (guid_el.text or "").strip() if guid_el is not None else ""

        # 标题可能包含 <a> 标签，提取纯文本
        raw_title = (title_el.text or "") if title_el is not None else ""
        title = re.sub(r"<[^>]+>", "", raw_title).strip()

        description = (desc_el.text or "").strip() if desc_el is not None else ""

        # 日期：格式如 "Apr 6, 2026 1:55pm"
        pub_date = ""
        if pub_el is not None and pub_el.text:
            m = re.search(r"\d{4}", pub_el.text)
            if m:
                # 尝试解析为标准日期
                try:
                    dt = datetime.strptime(pub_el.text.strip(), "%b %d, %Y %I:%M%p")
                    pub_date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    # 只提取年月日
                    m2 = re.search(r"(\w+ \d+, \d{4})", pub_el.text)
                    if m2:
                        try:
                            dt = datetime.strptime(m2.group(1), "%b %d, %Y")
                            pub_date = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass

        if link:
            items.append({
                "url": link,
                "guid": guid,
                "title": title,
                "description": description,
                "pub_date": pub_date,
            })

    return items


def _fetch_rss_items(api_cfg: dict) -> tuple[list[dict], list[ItemUnreadable]]:
    """拉取所有 RSS feed，去重后返回（文章列表，读不到的 feed）。一个 feed 读不到不拦着别的 feed，但要报出来。"""
    feeds = api_cfg.get("rss_feeds", RSS_FEEDS)
    max_articles = api_cfg.get("max_articles", 100)

    seen_urls = set()
    all_items, failed = [], []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml",
    }

    for feed_url in feeds:
        try:
            resp = transport.get(feed_url, headers=headers, timeout=30)
            resp.raise_for_status()
            items = _parse_rss(resp.content)
            logger.info(f"[FiercePharma] RSS {feed_url} 解析到 {len(items)} 条")
            for item in items:
                url = item["url"]
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_items.append(item)
        except Exception as e:  # noqa: BLE001 -- 原因写进读不到的记录
            failed.append(ItemUnreadable(feed_url, "FiercePharma RSS 读取失败：" + type(e).__name__ + "：" + (str(e).splitlines() or [""])[0][:120]))

    # 几个 feed 并成一个从新到旧的队列（同一天的保持 feed 里的先后）：增量读取“遇到已存的就停”要的是一条从新到旧的列表，
    # 不能第一个 feed 的最新几篇读过了就停，第二个 feed 里新的就漏掉。日期取不到的（空串）排最后。
    all_items.sort(key=lambda item: item["pub_date"], reverse=True)
    return all_items[:max_articles], failed


def _read_article(url: str) -> dict:
    """读一篇详情页，返回提取结果（标题、日期、正文）。网站对同一篇偶尔回 403（反爬），或页面没读全；等一会儿重读
    一次，两次都不行就抛 ItemUnreadable：这一篇不存版本，网址和原因记进这次读取的覆盖信息，接着读下一篇。
    以前这里用 RSS 的摘要充数，存下标题为空、正文两百字的半页，和整篇文章一样当成一个版本。
    打开页面之前等一下站点的限速（transport.wait）。"""
    why = ""
    for attempt in range(2):
        if attempt:
            time.sleep(RETRY_DELAY)
        transport.wait(url)
        try:
            page = StealthyFetcher.fetch(
                url,
                headless=True,
                network_idle=False,
                disable_resources=True,
                timeout=60000,
            )
        except Exception as e:  # noqa: BLE001 -- 原因写进下面的错误
            why = type(e).__name__ + "：" + (str(e).splitlines() or [""])[0][:120]
            continue
        if not (page.status and 200 <= page.status < 300):
            why = f"HTTP {page.status}"
            continue
        extracted = extract_fiercepharma_detail(page)
        if extracted["title"] and len(extracted["body_text"]) >= MIN_BODY_CHARS:
            if attempt:  # why 还是第一次的原因
                logger.warning(f"[FiercePharma] 重读一次后读全了（第一次：{why}） | {url}")
            return extracted
        why = f"页面没读全（标题 {len(extracted['title'])} 字，正文 {len(extracted['body_text'])} 字）"
    raise ItemUnreadable(url, f"FiercePharma 详情页读取失败（已重读一次）：{why}")


def iter_fiercepharma(api_cfg: dict) -> Iterator[dict]:
    """
    Fierce Pharma 混合采集器（RSS 列表 + Playwright 详情页）。
    作为站点配置的 api_fetcher，由 fetch.site_crawler 逐条取用。
    """
    items, failed_feeds = _fetch_rss_items(api_cfg)
    logger.info(f"[FiercePharma] 共 {len(items)} 篇文章待采集")
    yield from failed_feeds  # 读不到的 feed 记成读不到的条目，不能当成“那个 feed 没有文章”

    for item in items:
        url = item["url"]
        doc_uuid = make_uuid(url)

        try:
            extracted = _read_article(url)
        except ItemUnreadable as unread:
            yield unread  # 不是抛出：抛出会结束这个生成器。读取方（fetch.site_crawler）记下这一篇，再要下一篇
            continue
        title = extracted["title"]
        pub_date = extracted["publish_date"] or item["pub_date"]  # 日期页面上没有时用 RSS 的
        body_text = extracted["body_text"]

        yield {
            "uuid": doc_uuid,
            "title": title,
            "publish_date": pub_date,
            "markdown": f"# {title}\n\n{body_text}",
            "structured_data": {
                "title": title,
                "publish_date": pub_date,
                "category": "行业资讯",
                "summary": body_text[:500],
                "drug_name": "",
                "manufacturer": "",
            },
            "parse_method": "rss+stealthy",
            "attachment_links": [],
            "source_url": url,
            "source_site": "fiercepharma",
            "sniffed_meta": {},
        }
