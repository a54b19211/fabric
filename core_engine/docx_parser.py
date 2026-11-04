"""
Word 文档转 PDF（.doc / .docx → PDF：Windows 用 win32com，Linux 用 LibreOffice）。
processing.attachment_parse 在自部署 MinerU 不支持 .doc 且没有云端 token 时用它转换，再交 MinerU。
"""

import platform
import subprocess
import time
from pathlib import Path

from loguru import logger


# ── win32com 转 PDF（Windows）─────────────────────────────────────────────────

def _doc_to_pdf_win32com(file_path: str) -> str:
    """
    Windows: 用 win32com 将 .doc/.docx 转为 PDF，返回 PDF 路径。
    PDF 保存在同目录，文件名与原文件相同（扩展名改为 .pdf）。
    """
    import pythoncom
    import win32com.client

    src = Path(file_path).resolve()
    pdf_path = src.with_suffix(".pdf")

    last_error = None
    for attempt in range(3):
        pythoncom.CoInitialize()
        word = None
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            doc = None
            try:
                doc = word.Documents.Open(str(src))
                try:
                    doc.SaveAs2(FileName=str(pdf_path), FileFormat=17)
                except (AttributeError, Exception):
                    doc.SaveAs(FileName=str(pdf_path), FileFormat=17)
                doc.Close(False)
                doc = None
            except Exception as e:
                if doc is not None:
                    try:
                        doc.Close(False)
                    except Exception:
                        pass
                raise RuntimeError(f"win32com 转 PDF 失败: {e}") from e
            finally:
                if word is not None:
                    try:
                        word.Quit()
                    except Exception:
                        pass
                word = None
            break  # success
        except Exception as e:
            last_error = e
            logger.warning(f"[DOCX] win32com 第 {attempt + 1}/3 次尝试失败: {e}")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    else:
        raise RuntimeError(f"win32com 转 PDF 失败（重试 3 次后）: {last_error}") from last_error

    if not pdf_path.exists():
        raise RuntimeError(f"win32com 转换后 PDF 文件未生成: {pdf_path}")

    return str(pdf_path)


# ── LibreOffice 转 PDF（Linux）────────────────────────────────────────────────

def _doc_to_pdf_libreoffice(file_path: str) -> str:
    """
    Linux: 用 LibreOffice headless 将 .doc/.docx 转为 PDF，返回 PDF 路径。
    需要安装 libreoffice-headless。
    """
    src = Path(file_path).resolve()
    out_dir = src.parent
    pdf_path = src.with_suffix(".pdf")

    # 如果 PDF 已存在，先删除避免冲突
    if pdf_path.exists():
        pdf_path.unlink()

    cmd = [
        "soffice",
        "--headless",
        "--convert-to", "pdf",
        "--outdir", str(out_dir),
        str(src)
    ]

    logger.info(f"[DOCX] LibreOffice 转换命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "未知错误"
        raise RuntimeError(f"LibreOffice 转换失败 (code={result.returncode}): {error_msg}")

    if not pdf_path.exists():
        raise RuntimeError(f"LibreOffice 转换失败: PDF 文件未生成 {pdf_path}")

    return str(pdf_path)


# ── 跨平台转 PDF 统一入口 ─────────────────────────────────────────────────────

def _doc_to_pdf(file_path: str) -> str:
    """
    跨平台将 .doc/.docx 转为 PDF。
    Windows: win32com
    Linux: LibreOffice headless
    """
    system = platform.system()
    logger.info(f"[DOCX] 当前平台: {system}，转换文件: {file_path}")

    if system == "Windows":
        return _doc_to_pdf_win32com(file_path)
    else:
        return _doc_to_pdf_libreoffice(file_path)
