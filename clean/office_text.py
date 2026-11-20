"""Word 附件的本机读法：.docx 直读包里的 word/document.xml；.doc 先用 LibreOffice 转成 .docx 再读。

为什么要有：2026-11-17 起 MinerU 云端一直排队（账号侧），.docx/.doc 只有云端一条路，NMPA 200 个 .docx、73 个 .doc 三次都
失败（derived_clean_text 里 600 条 mineru_cloud:parser_failed，就是 200 个文件 × 3 次）。文件本身没有问题：.docx 是
一个 zip，正文就在 XML 里，不需要版面识别。

读法：按 document.xml 的顺序，一个段落（w:p）一块文字，一张表（w:tbl）一块 Markdown 表格（一行一行，合并格按 Word 存的
样子各占一格）；段落里的 w:tab 记成空格，w:br 记成换行。页眉页脚、批注、文本框里的字不读（不在正文流里）。
没有页码：Word 的分页要排版才知道，块定位用第几块（locator_kind=block，和 MinerU 读 .docx 一样）。
"""

from __future__ import annotations

import subprocess
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from clean.excel_parser import _rows_to_markdown

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CONVERT_TIMEOUT = 180  # LibreOffice 转一个 .doc 的上限（秒）


def _paragraph(p) -> str:
    parts = []
    for node in p.iter(_W + "t", _W + "tab", _W + "br", _W + "cr"):
        if node.tag == _W + "t":
            parts.append(node.text or "")
        elif node.tag == _W + "tab":
            parts.append(" ")
        else:
            parts.append("\n")
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


def _table(tbl) -> str:
    rows = []
    for tr in tbl.findall(_W + "tr"):  # 只取这张表自己的行；格子里嵌套的表格，字算在那一格里
        cells = []
        for tc in tr.findall(_W + "tc"):
            text = " ".join(_paragraph(p) for p in tc.iter(_W + "p"))
            cells.append(" ".join(text.split()))
        if any(cells):
            rows.append(cells)
    return _rows_to_markdown(rows) if rows else ""


def docx_blocks(path: str) -> list[dict]:
    """.docx → MinerU content_list 一样的块：[{"type": "text"|"table", "text"/"table_body": ...}]。"""
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    body = root.find(_W + "body")
    blocks = []
    for child in body if body is not None else []:
        if child.tag == _W + "p":
            text = _paragraph(child)
            if text:
                blocks.append({"type": "text", "text": text})
        elif child.tag == _W + "tbl":
            table = _table(child)
            if table:
                blocks.append({"type": "table", "table_body": table})
        elif child.tag == _W + "sdt":  # 内容控件（目录等）：里面的段落照读
            for p in child.iter(_W + "p"):
                text = _paragraph(p)
                if text:
                    blocks.append({"type": "text", "text": text})
    return blocks


def parse_docx(path: str) -> dict:
    """解析器约定的返回：{"success", "markdown", "content_list", "error"}。"""
    try:
        blocks = docx_blocks(path)
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError):
        return {"success": False, "markdown": "", "error": "not_a_docx"}
    markdown = "\n\n".join(b.get("text") or b.get("table_body") or "" for b in blocks)
    return {"success": True, "markdown": markdown, "content_list": blocks, "error": ""}


def doc_to_docx(path: str, work: str) -> str:
    """LibreOffice 把 .doc 转成 .docx，放在 work 目录里；用独立的用户配置目录，几个进程同时转不会抢同一个配置锁。"""
    profile = Path(work) / "lo-profile"
    subprocess.run(
        ["soffice", f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "docx",
         "--outdir", work, path],
        check=True, capture_output=True, timeout=CONVERT_TIMEOUT,
    )
    out = Path(work) / (Path(path).stem + ".docx")
    if not out.is_file():
        raise RuntimeError("libreoffice produced no docx")
    return str(out)


def parse_doc(path: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="doc-to-docx-") as work:
        try:
            converted = doc_to_docx(path, work)
        except (subprocess.SubprocessError, OSError, RuntimeError):
            return {"success": False, "markdown": "", "error": "libreoffice_failed"}
        return parse_docx(converted)
