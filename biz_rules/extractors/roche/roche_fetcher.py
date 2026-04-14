"""
Roche Media Releases RSS 采集器。

数据源：https://www.roche.com/med_news_xml.xml
  - 官方 RSS feed，包含完整正文（HTML），无需渲染详情页
  - 每条 <item> 含：title / pubDate / link / description（正文 HTML）/ guid

流程：
  1. httpx 拉取 RSS XML
  2. 解析 <item>，将 description HTML 转为纯文本 Markdown
  3. yield 标准记录 dict（与 iter_fiercepharma 输出格式一致）
"""

import re
import time
import uuid as _uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterator
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

RSS_URL = "https://www.roche.com/med_news_xml.xml"
REQUEST_DELAY = 1.0  # 礼貌延迟（秒）


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _make_uuid(identifier: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, identifier))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _HTMLToText(HTMLParser):
    """极简 HTML → 纯文本转换器，保留段落换行。"""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_tags = {"script", "style"}
        self._block_tags = {"p", "li", "h1", "h2", "h3", "h4", "br", "tr"}
        self._current_skip = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._skip_tags:
            self._current_skip = True
        if tag in self._block_tags:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._skip_tags:
            self._current_skip = False
        if tag in self._block_tags:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._current_skip:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # 合并多余空行
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _html_to_markdown(html: str) -> str:
    """将 RSS description 中的 HTML 转为可读 Markdown 文本。"""
    parser = _HTMLToText()
    parser.feed(html)
    return parser.get_text()


def _parse_pubdate(raw: str) -> str:
    """RFC 2822 日期 → ISO 8601 日期字符串（YYYY-MM-DD）。"""
    try:
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        return m.group() if m else ""


# ── 主采集函数 ────────────────────────────────────────────────────────────────

def iter_roche(api_cfg: dict) -> Iterator[dict]:
    """
    Roche Media Releases RSS 拉取器。

    api_cfg 字段（均可选）：
      - rss_url:      RSS 地址，默认 https://www.roche.com/med_news_xml.xml
      - max_articles: 最多处理条数，0 = 全量（默认 0）
      - request_delay: 请求间隔秒数（默认 1.0）
    """
    rss_url = api_cfg.get("rss_url", RSS_URL)
    max_articles = api_cfg.get("max_articles", 0)
    delay = api_cfg.get("request_delay", REQUEST_DELAY)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    # ── Step 1: 拉取 RSS ──────────────────────────────────────────────────────
    logger.info(f"[Roche] 拉取 RSS: {rss_url}")
    try:
        resp = httpx.get(rss_url, headers=headers, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        xml_bytes = resp.content
    except Exception as e:
        logger.error(f"[Roche] RSS 请求失败: {e}")
        return

    # ── Step 2: 解析 XML ──────────────────────────────────────────────────────
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.error(f"[Roche] RSS XML 解析失败: {e}")
        return

    channel = root.find("channel")
    if channel is None:
        logger.error("[Roche] RSS 缺少 <channel> 节点")
        return

    items = channel.findall("item")
    logger.info(f"[Roche] RSS 共 {len(items)} 条，max_articles={max_articles or '全量'}")

    if max_articles > 0:
        items = items[:max_articles]

    # ── Step 3: 逐条解析 yield ────────────────────────────────────────────────
    for idx, item in enumerate(items):
        title_el = item.find("title")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")
        desc_el = item.find("description")
        guid_el = item.find("guid")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        pub_raw = pubdate_el.text.strip() if pubdate_el is not None and pubdate_el.text else ""
        desc_html = desc_el.text if desc_el is not None and desc_el.text else ""
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else link

        if not title and not link:
            continue

        publish_date = _parse_pubdate(pub_raw)
        body_text = _html_to_markdown(desc_html)

        # 组装 Markdown
        md_lines = [f"# {title}", ""]
        if publish_date:
            md_lines += [f"**Date:** {publish_date}", ""]
        if link:
            md_lines += [f"**Source:** [{link}]({link})", ""]
        md_lines += [body_text]
        markdown = "\n".join(md_lines)

        doc_uuid = _make_uuid(guid or link or title)

        record = {
            "uuid": doc_uuid,
            "title": title,
            "publish_date": publish_date,
            "markdown": markdown,
            "structured_data": {
                "title": title,
                "publish_date": publish_date,
                "source_url": link,
                "category": "制药企业新闻",
                "manufacturer": "Roche",
            },
            "sniffed_meta": {},
            "attachment_links": [],
            "source_url": link,
            "source_site": "roche",
            "parse_method": "rss_xml",
        }

        logger.info(f"[Roche] [{idx + 1}/{len(items)}] {publish_date} | {title[:60]}")
        yield record

        if delay > 0 and idx < len(items) - 1:
            time.sleep(delay)
