"""
Roche Media Releases 采集器（Scrapling 版）

数据源：https://www.roche.com/media/releases
采集方式：
  1. 用 StealthyFetcher 抓取列表页（支持 hash 路由分页）
     URL 格式: https://www.roche.com/media/releases#/year/{year}/page/{page}
  2. 用 Fetcher（快速）抓取详情页正文，失败时降级到 StealthyFetcher
  3. 按年份从高到低遍历，每年按页递增，直到某页返回 0 条为止
"""

import re
import time
import uuid as _uuid
from typing import Iterator, Set

from loguru import logger
from scrapling.fetchers import StealthyFetcher

from biz_rules.cleaners.field.date_format import normalize_date
from biz_rules.history_models import HistoryError


def _make_uuid(source_url: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"roche:{source_url}"))


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    """移除标题末尾的日期模式。"""
    if not title:
        return ""
    pattern = r'\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\s*\S*$'
    cleaned = re.sub(pattern, "", title, flags=re.IGNORECASE)
    return cleaned.strip() if len(cleaned) >= 5 else title


def _extract_raw_date(text: str) -> str:
    """从文本中提取 'Month DD, YYYY' 格式的原始日期串（不标准化）。"""
    if not text:
        return ""
    m = re.search(
        r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}',
        text, flags=re.IGNORECASE
    )
    return m.group(0) if m else ""


def _extract_date(text: str) -> str:
    """从文本中提取 'Month DD, YYYY' 格式的日期并标准化为 YYYY-MM-DD。"""
    return normalize_date(_extract_raw_date(text))


def _is_non_html(url: str) -> bool:
    # .xls/.xlsx 已支持 MinerU 解析，不再跳过
    skip = ('.pdf', '.doc', '.docx', '.zip', '.jpg', '.jpeg', '.png', '.gif')
    return url.lower().endswith(skip)


# ---------------------------------------------------------------------------
# 列表页解析
# ---------------------------------------------------------------------------

def _parse_list_page(page, collected_urls: Set[str], year: int, page_num: int) -> list[dict]:
    """从 Scrapling page 对象中提取新闻条目。"""
    articles = []

    items = page.css("article, div.teaser, div.news-teaser, li.news-item, .card, .release-item")
    if not items:
        # 宽泛兜底：找所有带链接的卡片容器
        items = page.find_all(lambda e: e.tag in ("article", "li") and e.css("a"))

    for item in items:
        link_el = item.css("a")[0] if item.css("a") else None
        if not link_el:
            continue
        href = link_el.attrib.get("href", "")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.roche.com" + href
        if href in collected_urls or _is_non_html(href):
            continue
        collected_urls.add(href)

        # 标题
        title_el = (
            item.css("h3")[0] if item.css("h3") else
            item.css("h2")[0] if item.css("h2") else
            item.css(".title")[0] if item.css(".title") else
            item.css(".headline")[0] if item.css(".headline") else
            None
        )
        # 标题文字藏在 h3 > a > span 里面：.text 只取元素自己的文字，得到空串，要取里面所有文字（实际页面：
        # <h3><a><span class="teaser__title">标题<span class="teaser__icon">…</span></span></a></h3>）
        title_source = title_el if title_el is not None else link_el
        raw_title = " ".join(title_source.get_all_text(strip=True).split())
        title = _clean_title(raw_title)

        # 日期
        date_el = (
            item.css("time")[0] if item.css("time") else
            item.css(".date")[0] if item.css(".date") else
            None
        )
        pub_date = ""
        if date_el:
            pub_date = normalize_date(date_el.attrib.get("datetime", "") or date_el.text or "")
        if not pub_date:
            pub_date = _extract_date(item.get_all_text())
        if not pub_date:
            raw = _extract_raw_date(title)
            if raw:
                pub_date = normalize_date(raw)
                title = title.replace(raw, "").strip()

        articles.append({"title": title, "source_url": href, "publish_date": pub_date})

    if articles:
        logger.info(f"[Roche] 年份 {year} 第 {page_num} 页收集到 {len(articles)} 条")
    return articles


# ---------------------------------------------------------------------------
# 详情页抓取
# ---------------------------------------------------------------------------

def _fetch_detail(url: str, delay: float) -> str:
    """抓取详情页正文，使用 StealthyFetcher。"""
    if _is_non_html(url):
        return ""

    time.sleep(delay)

    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=False,
            disable_resources=True,
            timeout=60000,
        )
        if page.status and 200 <= page.status < 300:
            return _extract_body(page)
    except Exception as e:
        logger.error(f"[Roche] 详情页抓取失败: {url} | {e}")

    return ""


def _extract_body(page) -> str:
    """从 Scrapling page 对象提取正文文本。"""
    for sel in ("article", ".article-body", ".content", "main", ".press-release", ".news-detail"):
        els = page.css(sel)
        if els:
            text = els[0].get_all_text(ignore_tags=("script", "style"))
            if text and len(text) > 100:
                return text
    # 兜底：全页文本
    return page.get_all_text(ignore_tags=("script", "style", "nav", "footer", "header"))


# ---------------------------------------------------------------------------
# 主生成器
# ---------------------------------------------------------------------------

def iter_roche_web(api_cfg: dict) -> Iterator[dict]:
    """
    主生成器：遍历年份和页码，抓取列表页，再抓取详情页正文。
    作为 api_fetcher 被 iter_api_records 调用。
    """
    start_url = api_cfg.get("start_url", "https://www.roche.com/media/releases")
    max_articles = api_cfg.get("max_articles", 0)
    year_start = api_cfg.get("year_start", 2026)
    year_end = api_cfg.get("year_end", 2016)
    max_pages_per_year = api_cfg.get("max_pages_per_year", 100)
    delay = api_cfg.get("delay_between_requests", 2.0)          # 列表页间隔
    detail_delay = api_cfg.get("detail_delay", 1.0)              # 详情页间隔

    collected_urls: Set[str] = set()
    total_yielded = 0

    for year in range(year_start, year_end - 1, -1):
        logger.info(f"[Roche] 开始处理年份: {year}")

        for page_num in range(1, max_pages_per_year + 1):
            target_url = f"{start_url}#/year/{year}/page/{page_num}"
            logger.debug(f"[Roche] 列表页: {target_url}")

            try:
                page = StealthyFetcher.fetch(
                    target_url,
                    headless=True,
                    network_idle=True,
                    disable_resources=True,
                    timeout=60000,
                )
            except Exception as e:
                # 读不到列表页是失败，不是“这一年读完了”：原样抛出，运行记为失败并写明原因（以前 break，被当作读到底）
                logger.error(f"[Roche] 列表页抓取失败: {target_url} | {e}")
                raise

            if not page.status or not (200 <= page.status < 300):
                raise HistoryError("source_read_failed",
                                   f"Roche 列表页读取失败：{year} 年第 {page_num} 页返回 HTTP {page.status}（不当作读到底）")

            page_articles = _parse_list_page(page, collected_urls, year, page_num)
            if not page_articles:
                logger.info(f"[Roche] 年份 {year} 第 {page_num} 页无新数据，跳至下一年")
                break

            # 列表页抓完立即逐条抓详情页并 yield
            for art in page_articles:
                logger.info(f"[Roche] 详情页 {art['source_url']}")
                body = _fetch_detail(art["source_url"], detail_delay)

                markdown = f"# {art['title']}\n\n"
                if art["publish_date"]:
                    markdown += f"**Date:** {art['publish_date']}\n\n"
                markdown += f"**Source:** [{art['source_url']}]({art['source_url']})\n\n{body}"

                yield {
                    "uuid": _make_uuid(art["source_url"]),
                    "title": art["title"],
                    "publish_date": art["publish_date"],
                    "markdown": markdown,
                    "structured_data": {
                        "title": art["title"],
                        "publish_date": art["publish_date"],
                        "source_url": art["source_url"],
                        "category": "制药企业新闻",
                        "manufacturer": "Roche",
                    },
                    "sniffed_meta": {},
                    "attachment_links": [],
                    "source_url": art["source_url"],
                    "source_site": "roche",
                    "parse_method": "scrapling_stealthy",
                }

                total_yielded += 1
                if max_articles > 0 and total_yielded >= max_articles:
                    return

            time.sleep(delay)

        if max_articles > 0 and total_yielded >= max_articles:
            break
