"""
Fierce Pharma RSS + 详情页混合采集器。

流程：
  1. 通过 httpx 拉取 RSS XML，解析出文章 URL 和基本元数据
  2. 对每篇文章用 StealthyFetcher 渲染详情页，提取标题和正文（RSS 里的标题是空的，只有详情页有）
  3. yield 标准记录 dict（与 route_webpage 输出格式一致）
详情页没读全（403、超时、标题或正文缺失）：重读一次，还不行就抛 HistoryError，这一篇不存版本。
"""

import re
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Iterator
from xml.etree import ElementTree as ET

import httpx
from loguru import logger
from scrapling.fetchers import StealthyFetcher

from biz_rules.extractors.fiercepharma.fiercepharma import extract_fiercepharma_detail
from biz_rules.history_models import HistoryError

# RSS 命名空间
_DC_NS = "http://purl.org/dc/elements/1.1/"

RSS_FEEDS = [
    "https://www.fiercepharma.com/rss/xml",
    "https://www.fiercepharma.com/rss/Pharma/xml",
    "https://www.fiercepharma.com/rss/manufacturing/xml",
]

REQUEST_DELAY = 3.0  # 详情页请求间隔（秒）
RETRY_DELAY = 10.0  # 第一次没读全，隔多久重读一次（秒）
MIN_BODY_CHARS = 1000  # 正文比这短，当作页面没读全（整篇文章实测五千字上下）


def _make_uuid(identifier: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, identifier))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_rss(xml_bytes: bytes) -> list[dict]:
    """解析 RSS XML，返回文章基本信息列表。"""
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.error(f"[FiercePharma] RSS XML 解析失败: {e}")
        return items

    channel = root.find("channel")
    if channel is None:
        return items

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


def _fetch_rss_items(api_cfg: dict) -> list[dict]:
    """拉取所有 RSS feed，去重后返回文章列表。"""
    feeds = api_cfg.get("rss_feeds", RSS_FEEDS)
    max_articles = api_cfg.get("max_articles", 100)

    seen_urls = set()
    all_items = []

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
            resp = httpx.get(feed_url, headers=headers, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            items = _parse_rss(resp.content)
            logger.info(f"[FiercePharma] RSS {feed_url} 解析到 {len(items)} 条")
            for item in items:
                url = item["url"]
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_items.append(item)
        except Exception as e:
            logger.error(f"[FiercePharma] RSS 拉取失败: {feed_url} | {e}")

    return all_items[:max_articles]


def _read_article(url: str) -> dict:
    """读一篇详情页，返回提取结果（标题、日期、正文）。网站对同一篇偶尔回 403（反爬），或页面没读全；等一会儿重读
    一次，两次都不行就抛 HistoryError：这一篇不存版本，原因写进这次读取的错误里。以前这里用 RSS 的摘要充数，
    存下标题为空、正文两百字的半页，和整篇文章一样当成一个版本。"""
    why = ""
    for attempt in range(2):
        time.sleep(REQUEST_DELAY if attempt == 0 else RETRY_DELAY)
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
            return extracted
        why = f"页面没读全（标题 {len(extracted['title'])} 字，正文 {len(extracted['body_text'])} 字）"
    raise HistoryError("source_read_failed", f"FiercePharma 详情页读取失败（已重读一次）：{why} | {url}")


def iter_fiercepharma(api_cfg: dict) -> Iterator[dict]:
    """
    Fierce Pharma 混合采集器（RSS 列表 + Playwright 详情页）。
    作为 api_fetcher 被 iter_api_records 调用。
    """
    items = _fetch_rss_items(api_cfg)
    logger.info(f"[FiercePharma] 共 {len(items)} 篇文章待采集")

    for item in items:
        url = item["url"]
        doc_uuid = _make_uuid(url)

        extracted = _read_article(url)
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
            "timestamp": _now_iso(),
        }
