"""
路由分发器。

解析优先级：
  1. crawler_settings.SITE_PROFILES 中配置了 detail_extractor → CSS 特解（不调大模型）
  2. 未配置 / 特解结果为空 → Trafilatura + Qwen 通解兜底
"""

from loguru import logger
from prefect import task
import trafilatura

from config.crawler_settings import SITE_PROFILES
from biz_rules.prompts import get_structured_extraction_prompt
from biz_rules.cleaners import remove_extra_whitespace, clean_title, truncate_text
from core_engine.cloud_api_client import mineru_pdf_to_markdown, qwen_extract_structured
from core_engine.pdf_meta_sniffer import sniff_pdf_metadata


def _identify_site(url: str) -> str:
    """根据 URL 匹配站点标识符，未匹配返回空字符串。"""
    url_lower = url.lower()
    for site_key, profile in SITE_PROFILES.items():
        if profile["url_pattern"] in url_lower:
            return site_key
    return ""


@task(name="route_webpage")
def route_webpage(page_data: dict) -> dict:
    """
    网页解析路由：先尝试特解，特解为空则走通解。
    """
    url = page_data["url"]
    page = page_data["page"]
    doc_uuid = page_data["uuid"]
    site_key = _identify_site(url)

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

    # ---------- 特解 ----------
    extractor = SITE_PROFILES.get(site_key, {}).get("detail_extractor")
    if extractor:
        logger.info(f"[特解] {site_key}: {url}")
        extracted = extractor(page)
        body_text = extracted.get("body_text", "")
        if body_text:
            result["title"] = clean_title(extracted.get("title", ""))
            result["publish_date"] = extracted.get("publish_date", "")
            result["markdown"] = f"# {result['title']}\n\n{remove_extra_whitespace(body_text)}"
            result["parse_method"] = "css_extract"
            result["attachment_links"] = extracted.get("attachment_links", [])
            result["structured_data"] = {
                "title": result["title"],
                "publish_date": result["publish_date"],
                "category": "通告",
                "summary": body_text[:200],
            }
            return result
        logger.warning(f"[特解] 结果为空，降级通解: {url}")

    # ---------- 通解：Trafilatura + Qwen ----------
    logger.info(f"[通解] {url}")
    html_content = page.html_content if hasattr(page, "html_content") else str(page)
    extracted_text = trafilatura.extract(
        html_content,
        include_comments=False,
        include_tables=True,
        output_format="txt",
    )
    if not extracted_text:
        logger.warning(f"[通解] Trafilatura 提取失败: {url}")
        extracted_text = ""

    result["markdown"] = f"# 网页内容\n\n{remove_extra_whitespace(extracted_text)}"
    result["parse_method"] = "trafilatura+qwen"

    if extracted_text:
        prompt = get_structured_extraction_prompt(truncate_text(extracted_text))
        structured = qwen_extract_structured(prompt)
        result["structured_data"] = structured
        result["title"] = clean_title(structured.get("title", ""))
        result["publish_date"] = structured.get("publish_date", "")

    return result


@task(name="route_pdf")
def route_pdf(pdf_info: dict) -> dict:
    """PDF 解析：MinerU 转 Markdown → 嗅探元信息 → Qwen 结构化。"""
    doc_uuid = pdf_info["uuid"]
    pdf_path = pdf_info["local_path"]
    source_url = pdf_info.get("source_url", "")
    parent_uuid = pdf_info.get("parent_uuid", "")
    source_site = pdf_info.get("source_site", "local")

    result = {
        "uuid": doc_uuid,
        "title": "",
        "publish_date": "",
        "markdown": "",
        "structured_data": {},
        "parse_method": "mineru+qwen",
        "attachment_links": [],
        "source_url": source_url,
        "source_site": source_site,
        "parent_uuid": parent_uuid,
        "sniffed_meta": {},
    }

    logger.info(f"[PDF] MinerU 解析: {pdf_path}")
    mineru_result = mineru_pdf_to_markdown(pdf_path)
    if not mineru_result["success"]:
        logger.error(f"[PDF] MinerU 失败: {mineru_result['error']}")
        return result

    markdown = mineru_result["markdown"]
    result["markdown"] = markdown

    sniffed = sniff_pdf_metadata(markdown)
    result["sniffed_meta"] = sniffed
    if sniffed.get("sniffed_title"):
        result["title"] = sniffed["sniffed_title"]

    if markdown:
        prompt = get_structured_extraction_prompt(truncate_text(markdown))
        structured = qwen_extract_structured(prompt)
        result["structured_data"] = structured
        if not result["title"] and structured.get("title"):
            result["title"] = clean_title(structured["title"])
        result["publish_date"] = structured.get("publish_date", "")

    return result
