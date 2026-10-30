"""
路由分发器。

解析优先级：
  1. crawler_settings.SITE_PROFILES 中配置了 detail_extractor → CSS 特解
  2. 未配置 / 特解结果为空 → Trafilatura 通解兜底

爬虫阶段只负责：
  - 提取正文 Markdown
  - 提取 title / publish_date（从 CSS 特解或 Trafilatura metadata）
  - 不调用 LLM，不做结构化提取

LLM 实体抽取在后续阶段（flows/extraction_flow.py）单独处理。
"""

from loguru import logger
from prefect import task
import trafilatura

from config.crawler_settings import SITE_PROFILES
from biz_rules.cleaners import remove_extra_whitespace, clean_title
from core_engine.common.cloud_api_client import mineru_pdf_to_markdown
from core_engine.parsing.pdf_meta_sniffer import sniff_pdf_metadata
from core_engine.lineage.lineage_tracer import snapshot, record_step


def _identify_site(url: str) -> str:
    """根据 URL 匹配站点标识符，未匹配返回空字符串。"""
    url_lower = url.lower()
    for site_key, profile in SITE_PROFILES.items():
        pattern = profile.get("url_pattern", "")
        if pattern and pattern in url_lower:
            return site_key
    return ""


def _record_parse_lineage(parsed_result: dict, op: str = "") -> None:
    """在 parse 步骤结束时记录关键字段的血缘（输入为空，输出为解析结果）。"""
    empty_before = {k: "" for k in snapshot(parsed_result)}
    record_step(parsed_result, step="parse", before=empty_before, op=op)


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
            _record_parse_lineage(result, op="css_extract")
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

    _record_parse_lineage(result, op="trafilatura")
    return result


@task(name="route_pdf")
def route_pdf(pdf_info: dict) -> dict:
    """
    PDF 解析：MinerU 转 Markdown → 嗅探元信息。
    不调 LLM，title 从 MinerU 输出或文件名嗅探。
    """
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
        "parse_method": "mineru",
        "attachment_links": [],
        "source_url": source_url,
        "source_site": source_site,
        "parent_uuid": parent_uuid,
        "sniffed_meta": {},
    }

    logger.info(f"[PDF] MinerU 解析: {pdf_path}")
    mineru_result = mineru_pdf_to_markdown.fn(pdf_path)
    if not mineru_result["success"]:
        logger.error(f"[PDF] MinerU 失败: {mineru_result['error']}")
        return result

    markdown = mineru_result["markdown"]
    result["markdown"] = markdown

    # 从 Markdown 内容嗅探标题（规则匹配，无 LLM）
    sniffed = sniff_pdf_metadata(markdown)
    result["sniffed_meta"] = sniffed
    if sniffed.get("sniffed_title"):
        result["title"] = sniffed["sniffed_title"]

    result["structured_data"] = {
        "title": result["title"],
        "publish_date": "",
        "category": "",
        "summary": "",
    }

    _record_parse_lineage(result, op="mineru")
    return result
