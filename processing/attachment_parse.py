"""加工层：附件原件 → Markdown 正文（复用旧 ETL 的解析实现，按 MinerU 实际能力排顺序）。

11-04 实测自部署 MinerU 3.4.0（旧配置 APP.MINERU_API_BASE）：PDF、DOCX 原生解析；DOC、XLS 返回
“Unsupported file type”。MinerU 4.x 和云端 API 原生支持 DOC/PPT/XLS 等 Office 格式。所以：
- PDF / DOCX：自部署 MinerU 原生解析（core_engine.cloud_api_client._mineru_parse）；失败且配置了云端 token → 云端；
- DOC / PPT / PPTX：配置了云端 token → 云端原生解析（processing.mineru_cloud）；否则 LibreOffice 转 PDF
  （core_engine.docx_parser._doc_to_pdf，在临时目录里转）再交自部署 MinerU；
- XLS / XLSX：openpyxl / xlrd 直读成 Markdown 表格（core_engine.excel_parser.excel_to_markdown），表格不丢行；
- 扩展名未知（.bin）时按文件头判断（core_engine.scrapling_client._sniff_bytes）。
自部署升级到 4.x 后，DOC 直接走自部署即可，把 SELF_HOSTED_NATIVE 加上 .doc。

先核对原件 sha256 再解析；任何失败都返回带原因的状态，不抛出，也不把“没解析出来”当成“附件为空”。
结果按（附件版本, PROCESSOR_VERSION）存，见 db.derived_text。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

PROCESSOR_VERSION = "datafabric.attachment_parse.v1"
SELF_HOSTED_NATIVE = {".pdf", ".docx"}
CLOUD_NATIVE = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
CONVERTIBLE = {".doc", ".ppt", ".pptx"}
EXCEL_EXTS = {".xls", ".xlsx"}
SUPPORTED = SELF_HOSTED_NATIVE | CLOUD_NATIVE | EXCEL_EXTS


def mineru_base() -> str:
    """自部署 MinerU 地址：DATAFABRIC_MINERU_URL，未配置时沿用旧配置 APP.MINERU_API_BASE。"""
    value = os.environ.get("DATAFABRIC_MINERU_URL", "")
    if not value:
        from config.settings import APP

        value = APP.MINERU_API_BASE
    return value.rstrip("/")


def _mineru(path: str) -> dict:
    from core_engine.cloud_api_client import _mineru_parse

    return _mineru_parse(path, base=mineru_base())


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


def _result(status, *, text="", method=None, error=None, ext=None, attempts=()):
    return {
        "status": status,
        "text": text,
        "chars": len(text),
        "parse_method": method,
        "error": error,
        "file_ext": ext,
        "attempts": list(attempts),
        "processor_version": PROCESSOR_VERSION,
    }


def _plan(ext):
    """按格式排解析顺序：[(方法名, 调用)]。"""
    cloud = [("mineru_cloud", _mineru_cloud)] if _cloud_enabled() else []
    if ext in EXCEL_EXTS:
        return [("excel", lambda p: {"success": True, "markdown": _excel(p), "error": ""})]
    if ext in SELF_HOSTED_NATIVE:
        return [("mineru", _mineru)] + cloud
    if cloud:
        return cloud
    return [("libreoffice_pdf+mineru", lambda p: _mineru(_doc_to_pdf(p)))]


def parse_file(*, store_path: str, sha256: str, file_ext: str | None = None, **_ignored) -> dict:
    path = Path(store_path or "")
    if not store_path or not path.is_file():
        return _result("failed", error="original_missing", ext=file_ext)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != sha256:
        return _result("failed", error="original_digest_mismatch", ext=file_ext)
    ext = (file_ext or path.suffix or "").lower()
    if ext not in SUPPORTED:
        ext = _sniff(content)
    if ext not in SUPPORTED:
        return _result("unsupported", ext=ext)
    attempts, method, error = [], None, "parser_failed"
    with tempfile.TemporaryDirectory(prefix="attachment-parse-") as work:
        # 解析器（尤其 LibreOffice）会在输入文件旁写东西：一律在临时副本上做；文件名带正确后缀
        copy = Path(work) / ("original" + ext)
        shutil.copyfile(path, copy)
        for method, call in _plan(ext):
            try:
                answer = call(str(copy))
            except Exception:  # noqa: BLE001 -- 解析服务/转换器故障记为失败，换下一个
                answer = {"success": False, "markdown": "", "error": "exception"}
            text = (answer.get("markdown") or "") if answer.get("success") else ""
            if text.strip():
                attempts.append(method + ":ok")
                return _result("parsed", text=text, method=method, ext=ext, attempts=attempts)
            error = "empty_text" if answer.get("success") else "parser_failed"
            attempts.append(method + ":" + error)
    return _result("failed", method=method, error=error, ext=ext, attempts=attempts)
