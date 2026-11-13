"""NCCN 指南列表 + 详情页采集器。

流程：
  1. StealthyFetcher 渲染各分类列表页（JS 渲染后才有正确内容）
  2. 只取 URL 中 category=N 与当前分类编号匹配的链接，按 guideline_id 去重
  3. 对每条指南渲染详情页，按 section（Guidelines / Evidence Blocks / Frameworks /
     Guidelines for Patients / International）提取所有 PDF 链接 + 版本号
  4. yield 标准记录 dict（uuid/title/markdown/structured_data/attachment_links/...）
一份指南是一组（fetch.site_crawler 的分组读取）：每份开头给一个 GroupStart(组号)，从组号 start 读起，续读时前面
的指南不再渲染详情页，整个在 start 之前的分类连列表页也不渲染。组号 = 分类编号 × GROUP_SPAN + 它在这个分类列表里的序号。
"""

import re
from typing import Iterator

from loguru import logger

from fetch.extractors.nccn.nccn import extract_nccn_detail
from store.history_models import GroupStart, ItemUnreadable, make_uuid

BASE_URL = "https://www.nccn.org"
GROUP_SPAN = 1000  # 组号 = 分类编号 × GROUP_SPAN + 序号：续读只渲染它所在的那一个分类列表页（以前四页都渲染，一段 120 秒里占了一半）

# 分类编号 → (分类名, 列表页 URL)
CATEGORIES = {
    "1": ("Treatment by Cancer Type",                   f"{BASE_URL}/guidelines/category_1"),
    "2": ("Detection, Prevention, and Risk Reduction",  f"{BASE_URL}/guidelines/category_2"),
    "3": ("Supportive Care",                            f"{BASE_URL}/guidelines/category_3"),
    "4": ("Specific Populations",                       f"{BASE_URL}/guidelines/category_4"),
}

# 分类名 → 分类编号（反查）
_NAME_TO_NUM = {v[0]: k for k, v in CATEGORIES.items()}

# 列表页等到指南链接出现、详情页等到各 section 的小标题（h4）出现，内容就齐了：不等“网络空闲”（页面挂着很多第三方脚本，
# 空闲要一两分钟，实测一页 88-141 秒），也不加载图片、字体、样式（只要文字和链接）。
# 一次正常渲染 4-16 秒。www.nccn.org 解析到两个 Cloudflare 地址，从 zktest 连其中一个 TLS 握手永远不通（实测 50%-70% 的连接
# 卡住，卡住的最后报 net::ERR_TIMED_OUT，要 30-40 秒）：卡住的那次 20 秒就放弃，重来一次要重新启动浏览器（换一次域名解析，
# 换到好的地址）。scrapling 自己的 retries 在同一个浏览器里重试，一再落在同一个坏地址上，所以重试放在这里，最多 RENDER_TRIES 次。
# 设了 DATAFABRIC_PROXY_NCCN（站点配置的 proxy_env）时，渲染和 PDF 下载都经这个代理，由代理去解析和连接，不再落到那个坏地址上。
RENDER_TRIES = 6
RENDER_TIMEOUT_MS = 20000
LIST_READY = 'a[href*="guidelines-detail"]'
DETAIL_READY = "h4"


def _render(fetcher, url: str, ready_selector: str, proxy: str | None = None):
    """渲染一页：等 ready_selector 出现，不等网络空闲、不加载图片等资源。卡住就换个浏览器重来，RENDER_TRIES 次都不行才抛出。
    proxy 是站点的代理（没设是 None：直连）。"""
    options = {"proxy": proxy} if proxy else {}
    for attempt in range(1, RENDER_TRIES + 1):
        try:
            return fetcher.fetch(url, network_idle=False, disable_resources=True, wait_selector=ready_selector,
                                 timeout=RENDER_TIMEOUT_MS, retries=1, **options)
        except Exception:
            if attempt == RENDER_TRIES:
                raise
            logger.warning(f"[NCCN] 渲染 {url} 第 {attempt} 次没成功，重来")


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


def _read_item(fetcher, item: dict, proxy: str | None = None) -> dict:
    """读一条指南的详情页，返回完整的记录 dict（详情页的标题和版本盖过列表页的）。读不到就抛异常。"""
    detail_url = item["detail_url"]
    detail = extract_nccn_detail(_render(fetcher, detail_url, DETAIL_READY, proxy))
    title = detail["title"] or item["title"]
    version = detail["version"] or item["version"]
    # 主 PDF：Guidelines section 的第一个
    pdf_url = next((sec["pdfs"][0]["url"] for sec in detail["sections"]
                    if "guideline" in sec["section_name"].lower() and sec["pdfs"]), "")
    logger.info(f"[NCCN] ✓ {title!r} v{version} | {len(detail['sections'])} sections, {len(detail['attachment_links'])} PDFs")
    return {
        "uuid": make_uuid(detail_url),
        "guideline_id": item["guideline_id"],
        "title": title,
        "version": version,
        "category_name": item["category_name"],
        "source_url": detail_url,
        "pdf_url": pdf_url,
        "sections": detail["sections"],
        "attachment_links": detail["attachment_links"],
        "markdown": _build_markdown(title, version, item["category_name"], detail_url, detail["sections"]),
        "structured_data": {
            "title": title,
            "publish_date": _extract_year(version),
            "category": "临床指南",
            "summary": title,
            "drug_name": "",
            "manufacturer": "",
            "version": detail["version"],
            "pdf_url": pdf_url,
            "category_name": item["category_name"],
            "guideline_id": item["guideline_id"],
            "sections": [
                {"section_name": sec["section_name"], "pdf_count": len(sec["pdfs"]), "pdfs": sec["pdfs"]}
                for sec in detail["sections"]
            ],
        },
        "parse_method": "nccn_css_extract",
        "source_site": "nccn",
        "publish_date": _extract_year(version),  # 页面只有版本号（3.2026），用版本年份当发布日期（见站点配置）
    }


def iter_nccn(api_cfg: dict, start: int = 0) -> Iterator[dict]:
    """
    NCCN 指南采集器。

    api_cfg 字段：
      - categories:       要抓取的分类名列表，默认全部 4 个分类
      - max_per_category: 每个分类最多抓取条数，0 = 全量（默认 0）
      - proxy:            站点的代理（fetch.site_crawler 按站点配置的 proxy_env 填；None 直连）
    start：从哪个组号读起（分类编号 × GROUP_SPAN + 它在分类列表里的序号，从 0 起）。以前跨分类按 guideline_id 去重：续读不渲染
    前面的分类就去不了重，也没有必要：两个分类里的同一份指南详情页网址不同，各是一条记录（2026-11-13 实测前两个分类 69 份和 7 份
    指南没有一份重复）。
    """
    cat_names: list[str] = api_cfg.get("categories", [v[0] for v in CATEGORIES.values()])
    max_per_cat: int = api_cfg.get("max_per_category", 0)
    proxy = api_cfg.get("proxy")

    try:
        from scrapling.fetchers import StealthyFetcher
        fetcher = StealthyFetcher(auto_match=False)
    except ImportError:
        logger.error("[NCCN] scrapling 未安装，无法运行")
        return

    for cat_name in cat_names:
        cat_num = _NAME_TO_NUM.get(cat_name)
        if not cat_num:
            logger.warning(f"[NCCN] 未知分类名: {cat_name!r}，跳过")
            continue
        cat_url = CATEGORIES[cat_num][1]
        if (int(cat_num) + 1) * GROUP_SPAN <= start:
            continue  # 这个分类整个在 start 之前，上一段读过了

        logger.info(f"[NCCN] 渲染分类列表页: {cat_name} → {cat_url}")
        list_page = _render(fetcher, cat_url, LIST_READY, proxy)  # 渲染失败就让异常出去：整次读取失败，不能当成“这个分类没有指南”
        items = _parse_rendered_list(list_page, cat_num, cat_name)
        if not items:
            yield ItemUnreadable(cat_url, "分类列表页一条指南都没解析到，可能改版")
            continue

        if max_per_cat > 0:
            items = items[:max_per_cat]

        logger.info(f"[NCCN] 分类 '{cat_name}' 有效条目 {len(items)} 条")

        for index, item in enumerate(items):
            group = int(cat_num) * GROUP_SPAN + index
            if group < start:
                continue  # 前面的指南上一段读过了
            yield GroupStart(group)
            try:
                record = _read_item(fetcher, item, proxy)
            except Exception as e:  # noqa: BLE001 -- 原因写进读不到的记录
                # 读不到就是读不到：不存一个只有列表页信息的降级版本（以后读到全的，它就成了“被改过”的旧版本）；
                # 不是抛出：抛出会结束这个生成器。读取方记下这一条，再要下一条
                yield ItemUnreadable(item["detail_url"], "NCCN 详情页读取失败：" + type(e).__name__ + "：" + (str(e).splitlines() or [""])[0][:120])
                continue
            yield record
