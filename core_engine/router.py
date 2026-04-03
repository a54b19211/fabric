"""
路由分发器 —— 高低优双轨解析的调度中枢。

Fast Track (VIP通道)：匹配到已知站点 CSS 规则 → 硬解，不走大模型
Slow Track (兜底通道)：网页走 Trafilatura → Qwen；PDF 走 MinerU → Qwen
"""

from loguru import logger
from prefect import task
import trafilatura

from config.crawler_settings import SITE_PROFILES
from biz_rules.css_extractors import extract_nmpa_detail, extract_nmpa_list
from biz_rules.prompts import get_structured_extraction_prompt
from biz_rules.cleaners import remove_extra_whitespace, clean_title, truncate_text
from core_engine.cloud_api_client import mineru_pdf_to_markdown, qwen_extract_structured
from core_engine.pdf_meta_sniffer import sniff_pdf_metadata


# ======================== CSS 硬解函数注册表 ========================
# key: 站点标识符（同 crawler_settings.SITE_PROFILES 的 key）
# value: 对应的硬解函数
CSS_EXTRACTOR_REGISTRY = {
    "nmpa": extract_nmpa_detail,
}


def _identify_site(url: str) -> str:
    """根据 URL 匹配站点标识符，未匹配返回空字符串。"""
    url_lower = url.lower()
    for site_key, profile in SITE_PROFILES.items():
        if profile["url_pattern"] in url_lower:
            return site_key
    return ""


# ===================================================================
#  网页解析路由
# ===================================================================

@task(name="route_webpage")
def route_webpage(page_data: dict) -> dict:
    """
    网页解析路由：根据 URL 决定走 Fast Track 还是 Slow Track。

    参数:
        page_data: scrapling_client.fetch_page() 的返回值
                   {"page": 页面对象, "url": ..., "uuid": ..., "timestamp": ...}

    返回:
        dict: {
            "uuid": 幂等UUID,
            "title": 标题,
            "publish_date": 发布日期,
            "markdown": Markdown 全文,
            "structured_data": 结构化字段 dict,
            "parse_method": "css_extract" 或 "trafilatura+qwen",
            "attachment_links": 附件链接列表,
            "source_url": 原始URL,
            "source_site": 站点标识,
        }
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

    # ---------- Fast Track：CSS 硬解 ----------
    if site_key in CSS_EXTRACTOR_REGISTRY:
        logger.info(f"[Fast Track] 站点 {site_key}，走 CSS 硬解: {url}")
        extractor = CSS_EXTRACTOR_REGISTRY[site_key]
        extracted = extractor(page)

        result["title"] = clean_title(extracted.get("title", ""))
        result["publish_date"] = extracted.get("publish_date", "")
        body_text = extracted.get("body_text", "")
        result["markdown"] = f"# {result['title']}\n\n{remove_extra_whitespace(body_text)}"
        result["parse_method"] = "css_extract"
        result["attachment_links"] = extracted.get("attachment_links", [])

        # Fast Track 硬解的数据直接填充，不需要 Qwen
        result["structured_data"] = {
            "title": result["title"],
            "publish_date": result["publish_date"],
            "category": "通告",
            "summary": body_text[:200] if body_text else "",
        }

        return result

    # ---------- Slow Track：Trafilatura + Qwen ----------
    logger.info(f"[Slow Track] 走 Trafilatura 兜底: {url}")

    html_content = page.html_content if hasattr(page, "html_content") else str(page)
    extracted_text = trafilatura.extract(
        html_content,
        include_comments=False,
        include_tables=True,
        output_format="txt",
    )

    if not extracted_text:
        logger.warning(f"Trafilatura 提取失败: {url}")
        extracted_text = ""

    markdown = f"# 网页内容\n\n{remove_extra_whitespace(extracted_text)}"
    result["markdown"] = markdown
    result["parse_method"] = "trafilatura+qwen"

    # 调用 Qwen 提取结构化数据
    if extracted_text:
        prompt = get_structured_extraction_prompt(truncate_text(extracted_text))
        structured = qwen_extract_structured(prompt)
        result["structured_data"] = structured
        result["title"] = clean_title(structured.get("title", ""))
        result["publish_date"] = structured.get("publish_date", "")

    return result


# ===================================================================
#  PDF 解析路由（统一走 Slow Track）
# ===================================================================

@task(name="route_pdf")
def route_pdf(pdf_info: dict) -> dict:
    """
    PDF 解析路由：MinerU → 嗅探元信息 → Qwen 结构化。

    参数:
        pdf_info: scrapling_client.download_pdf() 或 scan_local_pdfs() 的返回值

    返回:
        dict: 同 route_webpage 的返回格式，额外包含 sniffed_meta 字段
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
        "parse_method": "mineru+qwen",
        "attachment_links": [],
        "source_url": source_url,
        "source_site": source_site,
        "parent_uuid": parent_uuid,
        "sniffed_meta": {},
    }

    # ---- 步骤 1：MinerU 将 PDF 转 Markdown ----
    logger.info(f"[PDF Slow Track] MinerU 解析: {pdf_path}")
    mineru_result = mineru_pdf_to_markdown(pdf_path)

    if not mineru_result["success"]:
        logger.error(f"MinerU 解析失败: {mineru_result['error']}")
        result["markdown"] = ""
        return result

    markdown = mineru_result["markdown"]
    result["markdown"] = markdown

    # ---- 步骤 2：嗅探 DOI / 期刊名 / 标题 ----
    sniffed = sniff_pdf_metadata(markdown)
    result["sniffed_meta"] = sniffed
    if sniffed.get("sniffed_title"):
        result["title"] = sniffed["sniffed_title"]

    # ---- 步骤 3：Qwen 结构化提取 ----
    if markdown:
        prompt = get_structured_extraction_prompt(truncate_text(markdown))
        structured = qwen_extract_structured(prompt)
        result["structured_data"] = structured

        if not result["title"] and structured.get("title"):
            result["title"] = clean_title(structured["title"])
        result["publish_date"] = structured.get("publish_date", "")

    return result
