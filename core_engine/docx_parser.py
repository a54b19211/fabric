"""
Word 文档解析器（.doc / .docx → Markdown）

优先级：
  1. win32com (Word COM) — 最准确，支持 .doc/.docx，需要 Windows + Word 已安装
  2. mammoth              — 纯 Python，仅支持 .docx (Open XML)
  3. 失败则返回空
"""

import uuid as _uuid
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger


def _make_uuid(seed: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, seed))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── win32com 解析（支持 .doc 和 .docx）────────────────────────────────────────

def _cell_text(cell) -> str:
    return cell.Range.Text.strip().rstrip("\r\x07")


def _table_to_md(table) -> str:
    num_rows = table.Rows.Count
    num_cols = table.Columns.Count
    rows = []
    for r in range(1, num_rows + 1):
        cells = []
        for c in range(1, num_cols + 1):
            try:
                cells.append(_cell_text(table.Cell(r, c)))
            except Exception:
                cells.append("")  # merged cell
        rows.append("| " + " | ".join(cells) + " |")
        if r == 1:
            rows.append("| " + " | ".join(["---"] * num_cols) + " |")
    return "\n".join(rows)


def _para_to_md(para) -> str:
    style = para.Style.NameLocal
    text = para.Range.Text.strip().rstrip("\r\x07")
    if not text:
        return ""
    if "标题 1" in style or style in ("Heading 1",):
        return f"# {text}"
    elif "标题 2" in style or style in ("Heading 2",):
        return f"## {text}"
    elif "标题 3" in style or style in ("Heading 3",):
        return f"### {text}"
    elif "标题 4" in style or style in ("Heading 4",):
        return f"#### {text}"
    return text


def _win32com_to_markdown(file_path: str) -> str:
    """用 Word COM 接口解析 .doc/.docx，返回 Markdown 字符串。"""
    import win32com.client

    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    try:
        doc_path = str(Path(file_path).resolve())
        doc = word.Documents.Open(doc_path)

        # 建立表格范围映射
        table_ranges = []
        table_mds = {}
        for tbl in doc.Tables:
            start = tbl.Range.Start
            end = tbl.Range.End
            table_ranges.append((start, end))
            table_mds[(start, end)] = _table_to_md(tbl)

        lines = []
        emitted_tables = set()
        for para in doc.Paragraphs:
            s = para.Range.Start
            matched = None
            for (ts, te) in table_ranges:
                if ts <= s < te:
                    matched = (ts, te)
                    break
            if matched:
                if matched not in emitted_tables:
                    lines.append("")
                    lines.append(table_mds[matched])
                    lines.append("")
                    emitted_tables.add(matched)
            else:
                md = _para_to_md(para)
                lines.append(md)

        doc.Close(False)
        return "\n".join(lines).strip()
    finally:
        word.Quit()


# ── mammoth 解析（仅 .docx）──────────────────────────────────────────────────

def _mammoth_to_markdown(file_path: str) -> str:
    import mammoth
    with open(file_path, "rb") as f:
        result = mammoth.extract_raw_text(f)
    return result.value.strip()


# ── 主解析函数 ────────────────────────────────────────────────────────────────

def _parse_doc(file_path: str) -> str:
    """
    尝试解析 .doc/.docx，返回 Markdown 文本。
    优先 win32com，失败则降级 mammoth（仅 .docx）。
    """
    # 优先 win32com
    try:
        return _win32com_to_markdown(file_path)
    except ImportError:
        pass  # win32com 不可用，降级
    except Exception as e:
        logger.warning(f"[DOCX] win32com 失败，尝试降级: {e}")

    # 降级：mammoth（仅支持 .docx）
    ext = Path(file_path).suffix.lower()
    if ext == ".docx":
        try:
            return _mammoth_to_markdown(file_path)
        except Exception as e:
            raise RuntimeError(f"mammoth 解析失败: {e}")

    raise RuntimeError("win32com 不可用且文件为 .doc 格式，无法解析")


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
        "parse_method": "win32com_doc",
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
