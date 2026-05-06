"""
Word 文档解析器（.doc / .docx → Markdown）

当前解析方式：MinerU（.doc 和 .docx 统一走同一套接口）

已注释的旧方案：
  1. win32com (Word COM) — 最准确，支持 .doc/.docx，需要 Windows + Word 已安装
  2. mammoth              — 纯 Python，仅支持 .docx (Open XML)
"""

import uuid as _uuid
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger

from core_engine.cloud_api_client import mineru_pdf_to_markdown


def _make_uuid(seed: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, seed))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── [旧方案已注释] win32com 解析 ─────────────────────────────────────────────

# def _cell_text(cell) -> str:
#     return cell.Range.Text.strip().rstrip("\r\x07")
#
#
# def _table_to_md(table) -> str:
#     num_rows = table.Rows.Count
#     num_cols = table.Columns.Count
#     rows = []
#     for r in range(1, num_rows + 1):
#         cells = []
#         for c in range(1, num_cols + 1):
#             try:
#                 cells.append(_cell_text(table.Cell(r, c)))
#             except Exception:
#                 cells.append("")
#         rows.append("| " + " | ".join(cells) + " |")
#         if r == 1:
#             rows.append("| " + " | ".join(["---"] * num_cols) + " |")
#     return "\n".join(rows)
#
#
# def _para_to_md(para) -> str:
#     style = para.Style.NameLocal
#     text = para.Range.Text.strip().rstrip("\r\x07")
#     if not text:
#         return ""
#     if "标题 1" in style or style in ("Heading 1",):
#         return f"# {text}"
#     elif "标题 2" in style or style in ("Heading 2",):
#         return f"## {text}"
#     elif "标题 3" in style or style in ("Heading 3",):
#         return f"### {text}"
#     elif "标题 4" in style or style in ("Heading 4",):
#         return f"#### {text}"
#     return text
#
#
# def _win32com_to_markdown(file_path: str) -> str:
#     import win32com.client
#     word = win32com.client.Dispatch("Word.Application")
#     word.Visible = False
#     try:
#         doc_path = str(Path(file_path).resolve())
#         doc = word.Documents.Open(doc_path)
#         table_ranges = []
#         table_mds = {}
#         for tbl in doc.Tables:
#             start = tbl.Range.Start
#             end = tbl.Range.End
#             table_ranges.append((start, end))
#             table_mds[(start, end)] = _table_to_md(tbl)
#         lines = []
#         emitted_tables = set()
#         for para in doc.Paragraphs:
#             s = para.Range.Start
#             matched = None
#             for (ts, te) in table_ranges:
#                 if ts <= s < te:
#                     matched = (ts, te)
#                     break
#             if matched:
#                 if matched not in emitted_tables:
#                     lines.append("")
#                     lines.append(table_mds[matched])
#                     lines.append("")
#                     emitted_tables.add(matched)
#             else:
#                 lines.append(_para_to_md(para))
#         doc.Close(False)
#         return "\n".join(lines).strip()
#     finally:
#         word.Quit()


# ── [旧方案已注释] mammoth 解析（仅 .docx）──────────────────────────────────

# def _mammoth_to_markdown(file_path: str) -> str:
#     import mammoth
#     with open(file_path, "rb") as f:
#         result = mammoth.extract_raw_text(f)
#     return result.value.strip()


# ── [旧方案已注释] win32com → mammoth 降级链 ─────────────────────────────────

# def _parse_doc_legacy(file_path: str) -> str:
#     try:
#         return _win32com_to_markdown(file_path)
#     except ImportError:
#         pass
#     except Exception as e:
#         logger.warning(f"[DOCX] win32com 失败，尝试降级: {e}")
#     ext = Path(file_path).suffix.lower()
#     if ext == ".docx":
#         try:
#             return _mammoth_to_markdown(file_path)
#         except Exception as e:
#             raise RuntimeError(f"mammoth 解析失败: {e}")
#     raise RuntimeError("win32com 不可用且文件为 .doc 格式，无法解析")


# ── win32com 转 PDF ───────────────────────────────────────────────────────────

def _doc_to_pdf(file_path: str) -> str:
    """
    用 win32com 将 .doc/.docx 转为 PDF，返回 PDF 路径。
    PDF 保存在同目录，文件名与原文件相同（扩展名改为 .pdf）。
    """
    import win32com.client

    src = Path(file_path).resolve()
    pdf_path = src.with_suffix(".pdf")

    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(str(src))
        try:
            doc.SaveAs2(str(pdf_path), FileFormat=17)  # Word 2013+
        except AttributeError:
            doc.SaveAs(str(pdf_path), FileFormat=17)   # 旧版 Word 降级
        doc.Close(False)
    finally:
        word.Quit()

    return str(pdf_path)


# ── MinerU 解析（当前方案：doc/docx → PDF → MinerU）─────────────────────────

def _parse_doc(file_path: str) -> str:
    """win32com 将 .doc/.docx 转 PDF，再交给 MinerU 解析。"""
    logger.info(f"[DOCX] win32com 转 PDF: {file_path}")
    pdf_path = _doc_to_pdf(file_path)
    logger.info(f"[DOCX] 转换完成，PDF: {pdf_path}")

    result = mineru_pdf_to_markdown(pdf_path)
    if not result["success"]:
        raise RuntimeError(f"MinerU 解析失败: {result['error']}")
    return result["markdown"]


# ── 对外接口 ──────────────────────────────────────────────────────────────────

def route_docx(att_info: dict, parent_parsed: dict | None = None) -> dict:
    """
    解析 .doc/.docx 附件，返回标准 parsed_result。
    若传入 parent_parsed，则继承父文档的元数据。
    """
    file_path = att_info.get("local_path", "")
    doc_uuid = att_info.get("uuid", _make_uuid(file_path))
    source_url = att_info.get("source_url", "")
    parent_uuid = att_info.get("parent_uuid", "")
    source_site = att_info.get("source_site", "")

    parent_title = ""
    parent_publish_date = ""
    parent_doc_type = ""
    parent_language = ""
    parent_source_url = ""
    if parent_parsed:
        parent_title = parent_parsed.get("title", "")
        parent_publish_date = parent_parsed.get("publish_date", "")
        parent_doc_type = parent_parsed.get("doc_type", "")
        parent_language = parent_parsed.get("language", "")
        parent_source_url = parent_parsed.get("source_url", "")
        if not source_site:
            source_site = parent_parsed.get("source_site", "")

    result = {
        "uuid": doc_uuid,
        "title": "",
        "publish_date": parent_publish_date,
        "markdown": "",
        "structured_data": {},
        "parse_method": "mineru_doc",
        "attachment_links": [],
        "source_url": source_url,
        "source_site": source_site,
        "parent_uuid": parent_uuid,
        "sniffed_meta": {},
        "timestamp": _now_iso(),
        "doc_type": parent_doc_type,
        "language": parent_language,
    }

    if not file_path or not Path(file_path).exists():
        logger.warning(f"[DOCX] 文件不存在: {file_path}")
        return result

    logger.info(f"[DOCX] 解析: {file_path}")
    try:
        markdown = _parse_doc(file_path)
    except Exception as e:
        logger.error(f"[DOCX] 解析失败: {file_path} | {e}")
        return result

    if not markdown:
        logger.warning(f"[DOCX] 提取内容为空: {file_path}")
        return result

    file_name = Path(file_path).stem
    title = file_name

    fm_lines = ["---"]
    fm_lines.append(f'title: "{title}"')
    if parent_publish_date:
        fm_lines.append(f"publish_date: {parent_publish_date}")
    if source_site:
        fm_lines.append(f"source_site: {source_site}")
    if parent_doc_type:
        fm_lines.append(f"doc_type: {parent_doc_type}")
    if parent_language:
        fm_lines.append(f"language: {parent_language}")
    if source_url:
        fm_lines.append(f"source_url: {source_url}")
    if parent_uuid:
        fm_lines.append(f"parent_uuid: {parent_uuid}")
    if parent_title:
        fm_lines.append(f'parent_title: "{parent_title}"')
    if parent_source_url:
        fm_lines.append(f"parent_source_url: {parent_source_url}")
    fm_lines.append("---")
    frontmatter = "\n".join(fm_lines)

    result["title"] = title
    result["markdown"] = f"{frontmatter}\n\n# {title}\n\n{markdown}"
    result["structured_data"] = {
        "title": title,
        "publish_date": parent_publish_date,
        "category": "",
        "summary": markdown[:200],
    }

    logger.info(f"[DOCX] 解析完成: {file_path}，字符数: {len(markdown)}")
    return result
