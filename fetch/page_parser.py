"""
路由分发器。

解析优先级：
  1. fetch.site_profiles.SITE_PROFILES 中配置了 detail_extractor → CSS 特解
  2. 未配置 / 特解结果为空 → Trafilatura 通解兜底

爬虫阶段只负责：
  - 提取正文 Markdown
  - 提取 title / publish_date（从 CSS 特解或 Trafilatura metadata）
  - 不调用 LLM，不做结构化提取

LLM 实体抽取在后续阶段（graph/extraction/extraction_flow.py）单独处理。
"""

import trafilatura
from loguru import logger
from prefect import task

from fetch.extractors.fields import clean_title, remove_extra_whitespace
from fetch.site_profiles import SITE_PROFILES


def _identify_site(url: str) -> str:
    """根据 URL 匹配站点标识符，未匹配返回空字符串。"""
    url_lower = url.lower()
    for site_key, profile in SITE_PROFILES.items():
        pattern = profile.get("url_pattern", "")
        if pattern and pattern in url_lower:
            return site_key
    return ""


@task(name="route_webpage")
def route_webpage(page_data: dict, site_key: str = None) -> dict:
    """
    网页解析路由：先尝试特解，特解为空则走通解。
    只提取正文和基本元数据（title/publish_date），不调 LLM。

    site_key 可选：由调用方显式传入，避免 URL 匹配错乱（如 phirda 子站）。
    """
    url = page_data["url"]
    page = page_data["page"]
    doc_uuid = page_data["uuid"]
    site_key = site_key or _identify_site(url)

    result = {
        "uuid": doc_uuid,
        "title": "",
        "publish_date": "",
        "markdown": "",
        "structured_data": {},
        "parse_method": "",
        "attachment_links": [],
        "source_url": url,
        "source_site": site_key,
    }

    # ---------- 特解（CSS 选择器，无需 LLM）----------
    extractor = SITE_PROFILES.get(site_key, {}).get("detail_extractor")
    if extractor:
        logger.info(f"[特解] {site_key}: {url}")
        extracted = extractor(page)
        body_text = extracted.get("body_text", "")
        attachment_links = extracted.get("attachment_links", [])
        has_body = bool(body_text and body_text.strip())
        has_attachment = bool(attachment_links)
        if has_body or has_attachment:
            result["title"] = clean_title(extracted.get("title", ""))
            result["publish_date"] = extracted.get("publish_date", "")
            result["markdown"] = f"# {result['title']}\n\n{remove_extra_whitespace(body_text)}" if has_body else ""
            result["parse_method"] = "css_extract"
            result["attachment_links"] = attachment_links
            result["structured_data"] = {
                "title": result["title"],
                "publish_date": result["publish_date"],
                "category": extracted.get("category", ""),
                "summary": "",
            }
            if extracted.get("extra"):
                result["extra"] = extracted["extra"]
            return result
        logger.warning(f"[特解] 结果为空，降级通解: {url}")

    # ---------- 通解：Trafilatura（纯规则，无 LLM）----------
    logger.info(f"[通解] {url}")
    html_content = page.html_content if hasattr(page, "html_content") else str(page)

    # 用 trafilatura 同时提取正文和元数据
    meta = trafilatura.extract_metadata(html_content)
    extracted_text = trafilatura.extract(
        html_content,
        include_comments=False,
        include_tables=True,
        output_format="txt",
    )
    if not extracted_text:
        logger.warning(f"[通解] Trafilatura 提取失败: {url}")
        extracted_text = ""

    # 从 trafilatura metadata 取 title / date（不调 LLM）
    title = ""
    publish_date = ""
    if meta:
        title = clean_title(meta.title or "")
        publish_date = str(meta.date or "")

    result["title"] = title
    result["publish_date"] = publish_date
    result["markdown"] = f"# {title}\n\n{remove_extra_whitespace(extracted_text)}" if title else remove_extra_whitespace(extracted_text)
    result["parse_method"] = "trafilatura"
    result["structured_data"] = {
        "title": title,
        "publish_date": publish_date,
        "category": "",
        "summary": "",
    }

    return result

