"""
网络采集客户端 —— 基于 Scrapling 的统一采集层。
封装页面抓取、PDF 下载、本地文件扫描等功能。
"""

import uuid
import time
from urllib.parse import urlparse, unquote
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger
from prefect import task

# 引入 Scrapling 的高级反爬抓取器
from scrapling.fetchers import StealthyFetcher

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
    仅允许 pdf/doc/docx 三种，其他统一落为 .bin（保底不丢文件）。
    """
    parsed = urlparse(file_url)
    raw_path = unquote(parsed.path or "")
    suffix = Path(raw_path).suffix.lower()
    if suffix in {".pdf", ".doc", ".docx"}:
        return suffix
    return ".bin"


# ---------------------------------------------------------------------------
#  Prefect Tasks
# ---------------------------------------------------------------------------

@task(
    name="fetch_page",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def fetch_page(url: str, headers: dict = None, delay: float = 1.0, wait_selector: str = None):
    """
    抓取单个网页，返回 scrapling 页面对象。

    参数:
        url:           目标网页 URL
        headers:       已废弃，保留参数签名兼容旧调用，不再传给 StealthyFetcher
                       （传自定义 headers 会破坏 StealthyFetcher 的内置浏览器指纹）
        delay:         请求前等待秒数（礼貌爬取，防止高并发被封）
        wait_selector: 等待该 CSS 选择器出现后再返回，用于 JS 动态渲染页面

    返回:
        dict: {"page": 页面对象, "url": url, "uuid": 幂等UUID, "timestamp": ISO时间}
    """
    if delay > 0:
        time.sleep(delay)

    # 不传自定义 headers —— StealthyFetcher 内置完整浏览器指纹，
    # 手动覆盖 User-Agent 反而会暴露爬虫特征，导致 412 反爬拦截。
    page = StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=True,       # 等待页面网络请求全部完成，解决 JS 动态渲染问题
        disable_resources=True,  # 禁止加载图片/字体/CSS，提速并降低反爬特征
        timeout=30000,
        wait_selector=wait_selector,  # 等待关键元素出现（None 则不等待）
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
) -> dict:
    """
    下载远程附件文件到 /data/raw/ 目录（支持 pdf/doc/docx）。
    """
    import httpx

    file_uuid = make_uuid(pdf_url)
    file_ext = _guess_attachment_ext(pdf_url)
    save_dir = APP.RAW_DIR / source_site
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f"{file_uuid}{file_ext}"

    # 幂等：如果文件已存在则跳过下载
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

    # 附件下载通常不需要浏览器渲染，用 httpx 更快更稳定
    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=120) as client:
        resp = client.get(pdf_url)
        resp.raise_for_status()
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