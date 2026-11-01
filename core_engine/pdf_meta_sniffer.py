"""
PDF 元信息嗅探器 —— 从 Markdown 文本头部用正则提取 DOI、期刊名、标题等。
这是"来源推断引擎"的核心组件，在调用置信度打分之前先跑这个。
"""

import re

from loguru import logger
from prefect import task


# ======================== 正则表达式模式 ========================

# DOI 格式: 10.xxxx/xxxxx （标准 DOI 前缀为 10.）
DOI_PATTERN = re.compile(
    r"(?:doi[:\s]*|https?://doi\.org/)?(10\.\d{4,9}/[^\s,;\"'<>]+)",
    re.IGNORECASE,
)

# 常见期刊引用格式中的期刊名提取
# 匹配如: "Published in: Nature Medicine" / "Journal: 药学学报" / "期刊：中国药学杂志"
JOURNAL_PATTERN = re.compile(
    r"(?:journal|期刊|刊名|published\s+in|出版)[:\s：]*([^\n,;，；。]{2,60})",
    re.IGNORECASE,
)

# 标题提取：取 Markdown 文本的第一个 # 标题
TITLE_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# ISSN 格式
ISSN_PATTERN = re.compile(r"ISSN[:\s]*(\d{4}-\d{3}[\dXx])", re.IGNORECASE)


@task(name="sniff_pdf_metadata")
def sniff_pdf_metadata(markdown_text: str) -> dict:
    """
    从 Markdown 文本中嗅探提取元信息。
    优先扫描前 2000 字符（文献头部通常包含来源信息）。

    参数:
        markdown_text: MinerU 解析出的 Markdown 全文

    返回:
        dict: {
            "doi": "10.xxxx/xxxxx" 或 "",
            "journal_name": "期刊名" 或 "",
            "sniffed_title": "嗅探到的标题" 或 "",
            "issn": "ISSN号" 或 "",
        }
    """
    # 重点扫描头部 2000 字符
    head_text = markdown_text[:2000] if len(markdown_text) > 2000 else markdown_text

    result = {
        "doi": "",
        "journal_name": "",
        "sniffed_title": "",
        "issn": "",
    }

    # --- 嗅探 DOI ---
    doi_match = DOI_PATTERN.search(head_text)
    if doi_match:
        result["doi"] = doi_match.group(1).rstrip(".")
        logger.debug(f"嗅探到 DOI: {result['doi']}")

    # --- 嗅探期刊名 ---
    journal_match = JOURNAL_PATTERN.search(head_text)
    if journal_match:
        result["journal_name"] = journal_match.group(1).strip()
        logger.debug(f"嗅探到期刊: {result['journal_name']}")

    # --- 嗅探标题 ---
    title_match = TITLE_PATTERN.search(head_text)
    if title_match:
        result["sniffed_title"] = title_match.group(1).strip()

    # --- 嗅探 ISSN ---
    issn_match = ISSN_PATTERN.search(head_text)
    if issn_match:
        result["issn"] = issn_match.group(1)

    # 如果头部没找到 DOI，再扫一遍全文（有些文献 DOI 在末尾参考文献里）
    if not result["doi"] and len(markdown_text) > 2000:
        doi_match_full = DOI_PATTERN.search(markdown_text)
        if doi_match_full:
            result["doi"] = doi_match_full.group(1).rstrip(".")
            logger.debug(f"全文扫描嗅探到 DOI: {result['doi']}")

    return result
