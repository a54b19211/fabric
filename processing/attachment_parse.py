"""加工层：附件原件 → 带定位的正文（复用旧 ETL 的解析实现，按 MinerU 实际能力排顺序）。

11-04 实测自部署 MinerU 3.4.0（旧配置 APP.MINERU_API_BASE）：PDF、DOCX 原生解析；DOC、XLS 返回
“Unsupported file type”。MinerU 4.x 和云端 API 原生支持 DOC/PPT/XLS 等 Office 格式。所以：
- PDF / DOCX：自部署 MinerU 原生解析（core_engine.cloud_api_client._mineru_parse）；失败且配置了云端 token → 云端；
- DOC / PPT / PPTX：配置了云端 token → 云端原生解析（processing.mineru_cloud）；否则 LibreOffice 转 PDF
  （core_engine.docx_parser._doc_to_pdf，在临时目录里转）再交自部署 MinerU；
- XLS / XLSX：openpyxl / xlrd 直读成 Markdown 表格（core_engine.excel_parser.excel_to_markdown）；
- 扩展名未知（.bin）时按文件头判断（core_engine.scrapling_client._sniff_bytes）；
- XML（PMC 全文 JATS）：processing.jats_text 直读结构，不调外部服务；不是 JATS 文章记 unsupported。
  加 XML 不改变其他格式的结果，此前也没有 .xml 附件（download_pdf 只产出 pdf/doc/docx/xls/xlsx/bin），
  所以处理器版本不升。

定位（Codex r59）：按格式给实际能给的定位，不强行编页码。
- 原生 PDF：MinerU content_list 的 page_idx → pdf_page；
- DOC/PPT 经转换（LibreOffice 或云端内部转换）得到的页码 → converted_page，注明不是原文件页码；
- DOCX 原生解析没有分页（11-04 实测所有块 page_idx=0）→ block（第几个内容块）；
- Excel → sheet；
- JATS → jats_section（节路径 + JATS 路径，没有页码）；
- 解析服务没给 content_list → locator_kind=unavailable，正文仍可用，但不能回指页码。
正文由 content_list 的块按顺序拼成，换页处插入“〔第N页〕”（只在有真实页码时），segments 记每块在正文里的
起止偏移和定位。

大小与耗时：原件超过 MAX_PARSE_BYTES 直接记 too_large，不送解析；每种解析方式各有超时。
v2：加了定位与大小上限，结果与 v1 不同，所以升处理器版本。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

PROCESSOR_VERSION = "datafabric.attachment_parse.v2"
SELF_HOSTED_NATIVE = {".pdf", ".docx"}
CLOUD_NATIVE = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
CONVERTIBLE = {".doc", ".ppt", ".pptx"}
EXCEL_EXTS = {".xls", ".xlsx"}
SUPPORTED = SELF_HOSTED_NATIVE | CLOUD_NATIVE | EXCEL_EXTS
JATS_EXTS = {".xml"}
SKIP_BLOCKS = {"page_number", "header", "footer", "page_header", "page_footer", "discarded"}


def max_parse_bytes() -> int:
    return int(os.environ.get("DATAFABRIC_ATTACHMENT_MAX_PARSE_BYTES", str(200 * 1024 * 1024)))


def mineru_base() -> str:
    """自部署 MinerU 地址：DATAFABRIC_MINERU_URL，未配置时沿用旧配置 APP.MINERU_API_BASE。"""
    value = os.environ.get("DATAFABRIC_MINERU_URL", "")
    if not value:
        from config.settings import APP

        value = APP.MINERU_API_BASE
    return value.rstrip("/")


def _mineru(path: str) -> dict:
    from core_engine.cloud_api_client import _mineru_parse

    return _mineru_parse(path, base=mineru_base(), content_list=True, timeout=300)


def _mineru_cloud(path: str) -> dict:
    from processing import mineru_cloud

    return mineru_cloud.parse(path)


def _cloud_enabled() -> bool:
    return bool(os.environ.get("DATAFABRIC_MINERU_CLOUD_TOKEN"))


def _doc_to_pdf(path: str) -> str:
    from core_engine.docx_parser import _doc_to_pdf

    return _doc_to_pdf(path)


def _excel(path: str) -> str:
    from core_engine.excel_parser import excel_to_markdown

    return excel_to_markdown(path)


def _sniff(content: bytes) -> str:
    from core_engine.scrapling_client import _sniff_bytes

    return _sniff_bytes(content)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result(status, *, text="", method=None, error=None, ext=None, attempts=(), segments=None, locator_kind=None):
    return {
        "status": status,
        "text": text,
        "chars": len(text),
        "parse_method": method,
        "error": error,
        "file_ext": ext,
        "attempts": list(attempts),
        "segments": segments or [],
        "locator_kind": locator_kind or ("unavailable" if status == "parsed" else None),
        "processor_version": PROCESSOR_VERSION,
    }


def _block_text(block: dict) -> str:
    kind = block.get("type")
    if kind == "table":
        caption = " ".join(block.get("table_caption") or [])
        body = block.get("table_body") or ""
        foot = " ".join(block.get("table_footnote") or [])
        return "\n".join(x for x in (caption, body, foot) if x)
    if kind == "image":
        return " ".join(block.get("image_caption") or [])
    if kind == "list":
        return "\n".join(str(x) for x in (block.get("list_items") or []))
    return str(block.get("text") or "")


def from_blocks(blocks, page_kind) -> tuple[str, list, str]:
    """content_list → (正文, segments, locator_kind)。page_kind 为 None 表示没有可信页码，按块定位。"""
    parts, segments, offset, page_seen = [], [], 0, None
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") in SKIP_BLOCKS:
            continue
        body = _block_text(block).strip()
        if not body:
            continue
        page = block.get("page_idx")
        if page_kind and isinstance(page, int) and page != page_seen:
            marker = f"〔第{page + 1}页〕"
            parts.append(marker)
            offset += len(marker) + 2
            page_seen = page
        locator = {"block": index}
        if page_kind and isinstance(page, int):
            locator["page"] = page + 1
        segments.append({"start": offset, "end": offset + len(body), "type": block.get("type"), **locator})
        parts.append(body)
        offset += len(body) + 2
    return "\n\n".join(parts), segments, (page_kind or "block")


def _sheets(markdown: str) -> list:
    out = []
    for match in re.finditer(r"^## (.+)$", markdown, flags=re.M):
        out.append({"start": match.start(), "sheet": match.group(1).strip()})
    for i, seg in enumerate(out):
        seg["end"] = out[i + 1]["start"] if i + 1 < len(out) else len(markdown)
    return out


def _plan(ext):
    """按格式排解析顺序：[(方法名, 调用, 页码种类)]。页码种类 None 表示没有可信页码。"""
    cloud_kind = "pdf_page" if ext == ".pdf" else "converted_page"
    cloud = [("mineru_cloud", _mineru_cloud, cloud_kind)] if _cloud_enabled() else []
    if ext in EXCEL_EXTS:
        return [("excel", lambda p: {"success": True, "markdown": _excel(p), "error": ""}, None)]
    if ext == ".pdf":
        return [("mineru", _mineru, "pdf_page")] + cloud
    if ext == ".docx":
        return [("mineru", _mineru, None)] + cloud
    if cloud:
        return cloud
    return [("libreoffice_pdf+mineru", lambda p: _mineru(_doc_to_pdf(p)), "converted_page")]


def _jats(path: Path, ext: str) -> dict:
    from processing import jats_text

    try:
        parsed = jats_text.parse(path)
    except (ValueError, OSError):
        return _result("failed", method="jats_xml", error="invalid_xml", ext=ext, attempts=["jats_xml:invalid_xml"])
    if parsed is None:
        return _result("unsupported", ext=ext, attempts=["jats_xml:not_jats"])
    if not parsed["text"].strip():
        return _result("failed", method="jats_xml", error="empty_text", ext=ext, attempts=["jats_xml:empty_text"])
    return _result("parsed", text=parsed["text"], method="jats_xml", ext=ext, attempts=["jats_xml:ok"],
                   segments=parsed["segments"], locator_kind=parsed["locator_kind"])


def parse_file(*, store_path: str, sha256: str, file_ext: str | None = None, **_ignored) -> dict:
    path = Path(store_path or "")
    if not store_path or not path.is_file():
        return _result("failed", error="original_missing", ext=file_ext)
    if path.stat().st_size > max_parse_bytes():
        return _result("failed", error="too_large", ext=file_ext)
    if _sha256_file(path) != sha256:
        return _result("failed", error="original_digest_mismatch", ext=file_ext)
    ext = (file_ext or path.suffix or "").lower()
    if ext in JATS_EXTS:
        return _jats(path, ext)
    if ext not in SUPPORTED:
        with path.open("rb") as stream:
            ext = _sniff(stream.read(4 * 1024 * 1024))
    if ext not in SUPPORTED:
        return _result("unsupported", ext=ext)
    attempts, method, error = [], None, "parser_failed"
    with tempfile.TemporaryDirectory(prefix="attachment-parse-") as work:
        # 解析器（尤其 LibreOffice）会在输入文件旁写东西：一律在临时副本上做；文件名带正确后缀
        copy = Path(work) / ("original" + ext)
        shutil.copyfile(path, copy)
        for method, call, page_kind in _plan(ext):
            try:
                answer = call(str(copy))
            except Exception:  # noqa: BLE001 -- 解析服务/转换器故障记为失败，换下一个
                answer = {"success": False, "markdown": "", "error": "exception"}
            markdown = (answer.get("markdown") or "") if answer.get("success") else ""
            if not markdown.strip():
                error = "empty_text" if answer.get("success") else "parser_failed"
                attempts.append(method + ":" + error)
                continue
            attempts.append(method + ":ok")
            blocks = answer.get("content_list")
            if method == "excel":
                return _result("parsed", text=markdown, method=method, ext=ext, attempts=attempts,
                               segments=_sheets(markdown), locator_kind="sheet")
            if isinstance(blocks, list) and blocks:
                text, segments, kind = from_blocks(blocks, page_kind)
                if text.strip():
                    return _result("parsed", text=text, method=method, ext=ext, attempts=attempts,
                                   segments=segments, locator_kind=kind)
            return _result("parsed", text=markdown, method=method, ext=ext, attempts=attempts,
                           locator_kind="unavailable")
    return _result("failed", method=method, error=error, ext=ext, attempts=attempts)
