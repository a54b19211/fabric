"""NCCN 指南列表 + 详情页采集器。

流程：
  1. StealthyFetcher 渲染各分类列表页（JS 渲染后才有正确内容）
  2. 只取 URL 中 category=N 与当前分类编号匹配的链接，按 guideline_id 去重
  3. 对每条指南渲染详情页，按 section（Guidelines / Evidence Blocks / Frameworks /
     Guidelines for Patients / International）提取所有 PDF 链接 + 版本号
  4. yield 标准记录 dict（uuid/title/markdown/structured_data/attachment_links/...）
"""

import re
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Iterator

from loguru import logger

from biz_rules.extractors.nccn.nccn import extract_nccn_detail
from biz_rules.history_models import ItemUnreadable

BASE_URL = "https://www.nccn.org"

# 分类编号 → (分类名, 列表页 URL)
CATEGORIES = {
    "1": ("Treatment by Cancer Type",                   f"{BASE_URL}/guidelines/category_1"),
    "2": ("Detection, Prevention, and Risk Reduction",  f"{BASE_URL}/guidelines/category_2"),
    "3": ("Supportive Care",                            f"{BASE_URL}/guidelines/category_3"),
    "4": ("Specific Populations",                       f"{BASE_URL}/guidelines/category_4"),
}

# 分类名 → 分类编号（反查）
_NAME_TO_NUM = {v[0]: k for k, v in CATEGORIES.items()}

REQUEST_DELAY = 1.5

# 列表页等到指南链接出现、详情页等到各 section 的小标题（h4）出现，内容就齐了：不等“网络空闲”（页面挂着很多第三方脚本，
# 空闲要一两分钟，实测一页 88-141 秒），也不加载图片、字体、样式（只要文字和链接）。
LIST_READY = 'a[href*="guidelines-detail"]'
DETAIL_READY = "h4"


def _render(fetcher, url: str, ready_selector: str):
    """渲染一页：等 ready_selector 出现，不等网络空闲、不加载图片等资源。"""
    return fetcher.fetch(url, network_idle=False, disable_resources=True, wait_selector=ready_selector,
                         timeout=45000, retries=2)


def _make_uuid(identifier: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, identifier))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_year(version: str) -> str:
    """从版本号 "3.2026" 提取年份 "2026"，提取失败返回空字符串。"""
    if not version:
        return ""
    m = re.search(r"(\d{4})", version)
    return m.group(1) if m else ""


def _parse_rendered_list(page, cat_num: str, category_name: str) -> list[dict]:
    """
    从已渲染的列表页中提取当前分类的指南条目。
    只取 URL 里 category=<cat_num> 的链接，按 guideline_id 去重。
    版本号直接从列表页文本解析（渲染后可见）。
    """
    items = []
    seen_ids: set = set()

    for a_el in page.css("a[href]"):
        href = a_el.attrib.get("href", "")
        if "guidelines-detail" not in href:
            continue

        # 只取当前分类的链接
        cat_m = re.search(r"category=(\d+)", href)
        if not cat_m or cat_m.group(1) != cat_num:
            continue

        id_m = re.search(r"id=(\d+)", href)
        if not id_m:
            continue
        guideline_id = id_m.group(1)

        if guideline_id in seen_ids:
            continue
        seen_ids.add(guideline_id)

        title = a_el.get_all_text(strip=True)
        if not title:
            continue

        if not href.startswith("http"):
            href = BASE_URL + href

        # 版本号：在 <a> 后面的文本里，形如 "Version: 1.2026"
        version = ""
        parent_text = a_el.parent.get_all_text(strip=True) if a_el.parent else ""
        after_title = parent_text[parent_text.find(title) + len(title):]
        ver_m = re.search(r"Version[:\s]+([\d.]+)", after_title[:60])
        if ver_m:
            version = ver_m.group(1)

        items.append({
            "title": title,
            "version": version,
            "detail_url": href,
            "guideline_id": guideline_id,
            "category_name": category_name,
        })

    return items


def _build_markdown(title: str, version: str, category_name: str,
                    detail_url: str, sections: list) -> str:
    """把详情页的分 section 内容组装成 Markdown。"""
    lines = [
        f"# {title}",
        "",
        f"**Category**: {category_name}",
        f"**Version**: {version}",
        f"**Source**: {detail_url}",
        "",
    ]
    for sec in sections:
        sec_name = sec["section_name"]
        pdfs = sec["pdfs"]
        if not pdfs:
            continue
        lines.append(f"## {sec_name}")
        lines.append("")
        for pdf in pdfs:
            ver_str = f" (Version {pdf['version']})" if pdf["version"] else ""
            lines.append(f"- [{pdf['label']}]({pdf['url']}){ver_str}")
        lines.append("")
    return "\n".join(lines)


def _list_only_markdown(item: dict, detail_url: str) -> str:
    """只有列表页的信息（没读详情页，或详情页读失败）时的最小 markdown。"""
    return (f"# {item['title']}\n\n**Version**: {item['version']}\n**Category**: {item['category_name']}\n"
            f"**Source**: {detail_url}\n")


def iter_nccn(api_cfg: dict) -> Iterator[dict]:
    """
    NCCN 指南采集器。

    api_cfg 字段：
      - categories:       要抓取的分类名列表，默认全部 4 个分类
      - max_per_category: 每个分类最多抓取条数，0 = 全量（默认 0）
      - fetch_detail:     是否抓取详情页（默认 True）
      - request_delay:    请求间隔秒数（默认 1.5）
    """
    cat_names: list[str] = api_cfg.get("categories", [v[0] for v in CATEGORIES.values()])
    max_per_cat: int = api_cfg.get("max_per_category", 0)
    fetch_detail: bool = api_cfg.get("fetch_detail", True)
    delay: float = api_cfg.get("request_delay", REQUEST_DELAY)

    try:
        from scrapling.fetchers import StealthyFetcher
        fetcher = StealthyFetcher(auto_match=False)
    except ImportError:
        logger.error("[NCCN] scrapling 未安装，无法运行")
        return

    seen_globally: set = set()   # 跨分类去重

    for cat_name in cat_names:
        cat_num = _NAME_TO_NUM.get(cat_name)
        if not cat_num:
            logger.warning(f"[NCCN] 未知分类名: {cat_name!r}，跳过")
            continue
        cat_url = CATEGORIES[cat_num][1]

        logger.info(f"[NCCN] 渲染分类列表页: {cat_name} → {cat_url}")
        list_page = _render(fetcher, cat_url, LIST_READY)  # 渲染失败就让异常出去：整次读取失败，不能当成“这个分类没有指南”
        items = _parse_rendered_list(list_page, cat_num, cat_name)
        if not items:
            yield ItemUnreadable(cat_url, "分类列表页一条指南都没解析到，可能改版")
            continue

        # 跨分类去重
        items = [it for it in items if it["guideline_id"] not in seen_globally]
        for it in items:
            seen_globally.add(it["guideline_id"])

        if max_per_cat > 0:
            items = items[:max_per_cat]

        logger.info(f"[NCCN] 分类 '{cat_name}' 有效条目 {len(items)} 条")

        for item in items:
            detail_url = item["detail_url"]
            doc_uuid = _make_uuid(detail_url)

            record: dict = {
                "uuid": doc_uuid,
                "guideline_id": item["guideline_id"],
                "title": item["title"],
                "version": item["version"],
                "category_name": item["category_name"],
                "source_url": detail_url,
                "crawl_timestamp": _now_iso(),
                "pdf_url": "",
                "sections": [],
                "attachment_links": [],
                "markdown": "",
                "structured_data": {},
                "parse_method": "nccn_list_only",
                "source_site": "nccn",
            }

            if fetch_detail:
                try:
                    time.sleep(delay)
                    detail_page = _render(fetcher, detail_url, DETAIL_READY)
                    detail = extract_nccn_detail(detail_page)

                    # 详情页数据覆盖列表页
                    if detail["title"]:
                        record["title"] = detail["title"]
                    if detail["version"]:
                        record["version"] = detail["version"]

                    # 主 PDF（Guidelines section 第一个）
                    for sec in detail["sections"]:
                        if "guideline" in sec["section_name"].lower() and sec["pdfs"]:
                            record["pdf_url"] = sec["pdfs"][0]["url"]
                            break

                    record["sections"] = detail["sections"]
                    record["attachment_links"] = detail["attachment_links"]
                    record["parse_method"] = "nccn_css_extract"
                    record["structured_data"] = {
                        "title": record["title"],
                        "publish_date": _extract_year(record["version"]),
                        "category": "临床指南",
                        "summary": record["title"],
                        "drug_name": "",
                        "manufacturer": "",
                        "version": detail["version"],
                        "pdf_url": record["pdf_url"],
                        "category_name": item["category_name"],
                        "guideline_id": item["guideline_id"],
                        "sections": [
                            {
                                "section_name": s["section_name"],
                                "pdf_count": len(s["pdfs"]),
                                "pdfs": s["pdfs"],
                            }
                            for s in detail["sections"]
                        ],
                    }
                    record["markdown"] = _build_markdown(
                        title=record["title"],
                        version=record["version"],
                        category_name=item["category_name"],
                        detail_url=detail_url,
                        sections=detail["sections"],
                    )

                    total_pdfs = len(detail["attachment_links"])
                    logger.info(
                        f"[NCCN] ✓ {record['title']!r} "
                        f"v{record['version']} | "
                        f"{len(detail['sections'])} sections, {total_pdfs} PDFs"
                    )

                except Exception as e:
                    logger.warning(f"[NCCN] 详情页抓取失败 ({detail_url}): {e}")
                    # 降级：用列表页信息构造最小 markdown
                    record["markdown"] = _list_only_markdown(item, detail_url)
            else:
                record["markdown"] = _list_only_markdown(item, detail_url)
                record["structured_data"] = {
                    "title": item["title"],
                    "publish_date": _extract_year(item["version"]),
                    "category": "临床指南",
                    "summary": item["title"],
                    "drug_name": "",
                    "manufacturer": "",
                    "version": item["version"],
                    "category_name": item["category_name"],
                    "guideline_id": item["guideline_id"],
                }

            record["publish_date"] = _extract_year(record["version"])  # 页面只有版本号（3.2026），用版本年份当发布日期（见站点配置）
            yield record
