"""
网络采集客户端 —— 基于 Scrapling 的统一采集层。
封装页面抓取、PDF 下载、本地文件扫描等功能。
"""

import io
import uuid
import re
import time
import zipfile
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


def _sniff_bytes(content: bytes) -> str:
    """通过文件头魔数判断真实类型。"""
    if len(content) < 8:
        return ".bin"
    # HTML 错误页检测
    if content[:15].lower().startswith(b"<!doctype") or content[:6].lower().startswith(b"<html"):
        logger.warning(f"响应内容疑似 HTML 页面，非真实文件（前 100 字节: {content[:100]!r}）")
        return ".bin"
    header = content[:8]
    if header[:4] == b"%PDF":
        return ".pdf"
    if header[:4] == b"\xd0\xcf\x11\xe0":
        return ".doc"
    if header[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
                names = zf.namelist()
            if any(n.startswith("xl/") for n in names):
                return ".xlsx"
            if any(n.startswith("word/") for n in names):
                return ".docx"
        except Exception:
            pass
        return ".docx"
    return ".bin"


def _url_suffix(url: str) -> str:
    """从 URL 路径提取后缀（仅已知类型）。"""
    parsed = urlparse(url)
    raw_path = unquote(parsed.path or "")
    suffix = Path(raw_path).suffix.lower()
    if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx"}:
        return suffix
    return ""


def _ext_from_response(resp, url: str = "") -> str:
    """从 HTTP 响应推断文件扩展名。
    优先级：Content-Disposition → Content-Type → URL 后缀 → .bin
    注意：魔数嗅探不在这里做，因为可能和 URL 后缀冲突（如 NMPA 的 .doc 实为 OLE2），
    由调用方在 .bin 时用 _sniff_bytes 兜底。
    """
    # 1. Content-Disposition: attachment; filename="xxx.docx"
    cd = resp.headers.get("content-disposition", "")
    if cd:
        m = re.search(r'filename[^;=\n]*=[\s"\']*([^\s;"\']+)', cd, re.I)
        if m:
            suffix = Path(m.group(1)).suffix.lower()
            if suffix in {".pdf", ".doc", ".docx", ".xls", ".xlsx"}:
                return suffix

    # 2. Content-Type
    ct = resp.headers.get("content-type", "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "wordprocessingml" in ct:
        return ".docx"
    if "msword" in ct:
        return ".doc"
    if "spreadsheetml" in ct:
        return ".xlsx"
    if "ms-excel" in ct:
        return ".xls"

    # 3. URL 后缀
    if url:
        suffix = _url_suffix(url)
        if suffix:
            return suffix

    logger.warning(
        f"无法识别文件类型，Content-Disposition={resp.headers.get('content-disposition', 'N/A')!r}, "
        f"Content-Type={resp.headers.get('content-type', 'N/A')!r}, url={url}"
    )
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

    cookies: list[dict] = []

    if use_simple_fetch:
        page = Fetcher.get(
            url,
            stealthy_headers=True,
            follow_redirects=True,
            timeout=30,
        )
    else:
        def _collect_cookies(pw_page):
            try:
                cookies.extend(pw_page.context.cookies())
            except Exception:
                pass

        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=network_idle,
            disable_resources=disable_resources,
            timeout=30000,
            wait_selector=wait_selector,
            solve_cloudflare=solve_cloudflare,
            page_action=_collect_cookies,
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
        "cookies": cookies,
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
    cookies: list[dict] | None = None,
) -> dict:
    """
    下载远程附件文件到 /data/raw/ 目录（支持 pdf/doc/docx/xls/xlsx）。

    参数:
        force:   True 时跳过幂等检查，强制重新下载（重爬场景使用）
        cookies: 从抓取页面继承的浏览器 cookies（Playwright 格式），
                 用于需要会话的站点（如 CDE）
    """
    file_uuid = make_uuid(pdf_url)
    save_dir = APP.RAW_DIR / source_site
    save_dir.mkdir(parents=True, exist_ok=True)

    headers = dict(DEFAULT_HEADERS)
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies if "name" in c
        )

    with httpx.Client(headers=headers, follow_redirects=True, timeout=120) as client:
        resp = client.get(pdf_url)
        resp.raise_for_status()

    # 从响应推断扩展名（Content-Disposition → Content-Type → 魔数 → URL 后缀 → .bin）
    file_ext = _ext_from_response(resp, pdf_url)
    save_path = save_dir / f"{file_uuid}{file_ext}"

    if save_path.exists() and not force:
        logger.info(f"附件已存在，跳过保存: {save_path}")
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