"""
网络采集客户端 —— 基于 Scrapling 的统一采集层。
封装页面抓取、PDF 下载、本地文件扫描等功能。
"""

import uuid
import re
import time
from urllib.parse import urlparse, unquote
from pathlib import Path
from datetime import datetime, timezone

import httpx

from loguru import logger
from prefect import task

# 引入 Scrapling 的高级反爬抓取器
from scrapling.fetchers import StealthyFetcher, Fetcher

from config.settings import APP
from config.crawler_settings import DEFAULT_HEADERS


# ---------------------------------------------------------------------------
#  工具函数
# ---------------------------------------------------------------------------

def make_uuid(identifier: str) -> str:
    """基于 URL 或文件路径生成确定性 UUID5，保证幂等。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identifier))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guess_attachment_ext(file_url: str) -> str:
    """
    从下载 URL 猜测文件扩展名。
    仅允许已知类型，其他返回空字符串（表示需要从响应头推断）。
    """
    parsed = urlparse(file_url)
    raw_path = unquote(parsed.path or "")
    suffix = Path(raw_path).suffix.lower()
    if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx"}:
        return suffix
    return ""   # 空字符串表示需要从响应头推断


def _ext_from_response(resp) -> str:
    """从 HTTP 响应头推断文件扩展名。"""
    # Content-Disposition: attachment; filename="xxx.docx"
    cd = resp.headers.get("content-disposition", "")
    if cd:
        m = re.search(r'filename[^;=\n]*=[\s"\']*([^\s;"\']+)', cd, re.I)
        if m:
            suffix = Path(m.group(1)).suffix.lower()
            if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx"}:
                return suffix

    # Content-Type
    ct = resp.headers.get("content-type", "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "wordprocessingml" in ct or "msword" in ct:
        return ".docx"
    if "spreadsheetml" in ct:
        return ".xlsx"
    if "ms-excel" in ct or "xls" in ct:
        return ".xls"

    return ".bin"


# ---------------------------------------------------------------------------
#  Prefect Tasks
# ---------------------------------------------------------------------------

@task(
    name="fetch_page",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def fetch_page(url: str, headers: dict = None, delay: float = 1.0, wait_selector: str = None, disable_resources: bool = True, solve_cloudflare: bool = False, network_idle: bool = True, use_simple_fetch: bool = False):
    """
    抓取单个网页，返回 scrapling 页面对象。

    参数:
        url:               目标网页 URL
        headers:           已废弃，保留参数签名兼容旧调用
        delay:             请求前等待秒数
        wait_selector:     等待该 CSS 选择器出现后再返回
        disable_resources: 是否禁止加载图片/字体/CSS（默认 True 提速）
        network_idle:      是否等待网络空闲后再返回（默认 True）
        use_simple_fetch:  True = 用 httpx 直接请求（StaticFetcher），跳过 Playwright
                           适合无反爬、无 JS 渲染需求的站点，速度快 10x
    """
    if delay > 0:
        time.sleep(delay)

    if use_simple_fetch:
        page = Fetcher.get(
            url,
            stealthy_headers=True,
            follow_redirects=True,
            timeout=30,
        )
    else:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=network_idle,
            disable_resources=disable_resources,
            timeout=30000,
            wait_selector=wait_selector,
            solve_cloudflare=solve_cloudflare,
        )

    logger.info(f"页面抓取成功: {url} | 状态码: {page.status}")

    # 非 2xx 状态码视为失败，抛出异常让 Prefect 重试机制接管
    if not (200 <= page.status < 300):
        raise RuntimeError(f"HTTP {page.status}，页面请求被拒绝: {url}")

    return {
        "page": page,
        "url": url,
        "uuid": make_uuid(url),
        "timestamp": _now_iso(),
    }


@task(
    name="download_pdf",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def download_pdf(
    pdf_url: str,
    parent_uuid: str = "",
    source_site: str = "",
    force: bool = False,
) -> dict:
    """
    下载远程附件文件到 /data/raw/ 目录（支持 pdf/doc/docx/xls/xlsx）。

    参数:
        force: True 时跳过幂等检查，强制重新下载（重爬场景使用）
    """
    import httpx

    file_uuid = make_uuid(pdf_url)
    file_ext = _guess_attachment_ext(pdf_url)  # 可能为空，需下载后从响应头确认
    save_dir = APP.RAW_DIR / source_site
    save_dir.mkdir(parents=True, exist_ok=True)

    # 如果 URL 能推断扩展名，先检查幂等（force=True 时跳过）
    if file_ext and not force:
        save_path = save_dir / f"{file_uuid}{file_ext}"
        if save_path.exists():
            logger.info(f"附件已存在，跳过下载: {save_path}")
            return {
                "uuid": file_uuid,
                "local_path": str(save_path),
                "source_url": pdf_url,
                "parent_uuid": parent_uuid,
                "source_site": source_site,
                "file_ext": file_ext,
                "timestamp": _now_iso(),
                "skipped": True,
            }

    # 下载文件
    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=120) as client:
        resp = client.get(pdf_url)
        resp.raise_for_status()

        # URL 没有扩展名时，从响应头推断
        if not file_ext:
            file_ext = _ext_from_response(resp)
            save_path = save_dir / f"{file_uuid}{file_ext}"
            if save_path.exists() and not force:
                logger.info(f"附件已存在，跳过下载: {save_path}")
                return {
                    "uuid": file_uuid,
                    "local_path": str(save_path),
                    "source_url": pdf_url,
                    "parent_uuid": parent_uuid,
                    "source_site": source_site,
                    "file_ext": file_ext,
                    "timestamp": _now_iso(),
                    "skipped": True,
                }
        else:
            save_path = save_dir / f"{file_uuid}{file_ext}"

        save_path.write_bytes(resp.content)

    logger.info(f"附件下载完成: {pdf_url} → {save_path}")

    return {
        "uuid": file_uuid,
        "local_path": str(save_path),
        "source_url": pdf_url,
        "parent_uuid": parent_uuid,
        "source_site": source_site,
        "file_ext": file_ext,
        "timestamp": _now_iso(),
        "skipped": False,
    }


@task(name="scan_local_pdfs")
def scan_local_pdfs(directory: str) -> list:
    """
    批量扫描本地目录中的 PDF 文件。
    """
    pdf_dir = Path(directory)
    if not pdf_dir.exists():
        logger.warning(f"扫描目录不存在: {directory}")
        return []

    results = []
    for pdf_file in sorted(pdf_dir.rglob("*.pdf")):
        file_uuid = make_uuid(str(pdf_file))
        results.append({
            "uuid": file_uuid,
            "local_path": str(pdf_file),
            "source_url": "",
            "parent_uuid": "",
            "source_site": "local",
            "timestamp": _now_iso(),
        })

    logger.info(f"扫描到 {len(results)} 个本地 PDF: {directory}")
    return results