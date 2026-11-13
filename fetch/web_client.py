"""
网络采集客户端 —— 基于 Scrapling 的统一采集层。
封装页面抓取、PDF 下载、本地文件扫描等功能。
"""

import hashlib
import io
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from loguru import logger

# 引入 Scrapling 的高级反爬抓取器
from scrapling.fetchers import Fetcher, StealthyFetcher

from fetch import transport
from fetch.site_profiles import DEFAULT_HEADERS
from settings import APP
from store.history_models import make_uuid

# ---------------------------------------------------------------------------
#  工具函数
# ---------------------------------------------------------------------------

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
        except Exception as e:
            logger.warning(f"[Scrapling] MIME 嗅探失败，回退 .docx: {e}")
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


def _file_name_from_response(resp) -> str:
    """站点在 Content-Disposition 里给的文件名，如 attachment;filename=4C3F5162.xlsx（没有就是空串）。"""
    found = re.search(r'filename\s*=\s*"?([^";]+)', resp.headers.get("content-disposition", ""), re.I)
    return Path(found.group(1).strip()).name if found else ""


def _ext_from_response(resp, url: str = "") -> str:
    """从 HTTP 响应推断文件扩展名。
    优先级：Content-Disposition → Content-Type → URL 后缀 → .bin
    注意：魔数嗅探不在这里做，因为可能和 URL 后缀冲突（如 NMPA 的 .doc 实为 OLE2），
    由调用方在 .bin 时用 _sniff_bytes 兜底。内容类型是网页（html）的给 .html。
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
    if "html" in ct:  # 网页（港交所有的公告附件是 .htm 页面）：按内容类型给 .html，不当成未知文件存成 .bin
        return ".html"

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


def _original_matches(path: Path, digest: str) -> bool:
    calculated = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            calculated.update(chunk)
    return calculated.hexdigest() == digest


def _sync_original_directories(directory: Path) -> None:
    """确认文件名和新增祖先目录的条目都已同步；重试也不能跳过。"""
    directory = directory.absolute()
    while True:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if directory.parent == directory:
            break
        directory = directory.parent


def _save_original(path: Path, content: bytes, digest: str) -> bool:
    """校验原件并同步文件/目录后才返回；不覆盖旧文件。"""
    reused = path.exists()
    temporary = None
    try:
        if reused:
            if not _original_matches(path, digest):
                raise OSError(f"原件摘要不符，未覆盖: {path}")
        else:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".download-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # 同目录硬链接只在目标不存在时成功；并发请求不会覆盖原件。
                os.link(temporary, path)
            except FileExistsError:
                if not _original_matches(path, digest):
                    raise OSError(f"原件摘要不符，未覆盖: {path}")
                reused = True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    # 三条成功路径共用此处。mkdir/link遗留的目录和文件即使已存在，
    # 也不能证明前一次已同步成功；每次同步整条祖先链，失败直接上抛。
    _sync_original_directories(path.parent)
    return reused


def store_original(content: bytes, identifier: str, source_site: str, file_ext: str) -> dict:
    """按字节摘要固定一份原件：<RAW_DIR>/<来源>/<uuid5(身份)>.<sha256><后缀>，校验、fsync、不覆盖。
    远程附件（download_pdf）、PMC 全文（fetch.pubmed_fulltext）、本地导入（fetch.local_files）
    共用这一种落盘写法。skipped=True 表示本地已有相同字节。"""
    if not content:
        raise ValueError("原件为空")
    save_dir = APP.RAW_DIR / source_site
    save_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    save_path = save_dir / f"{make_uuid(identifier)}.{digest}{file_ext}"
    skipped = _save_original(save_path, content, digest)
    return {"local_path": str(save_path), "sha256": digest, "size": len(content), "skipped": skipped}


# ---------------------------------------------------------------------------
#  抓页面、下附件
# ---------------------------------------------------------------------------

# 简单请求（use_simple_fetch）的超时（连接秒数，读取秒数）和次数（次数 = 最多发几次，scrapling 的 retries 就是这个意思）。
# 正常的页面一两秒就回来；个别节点会让连接或握手卡住：实测 zktest 到 fda.gov 的 Akamai 节点（23.35.148.x 一组，
# 解析会随时间换组）有时一半以上的新连接卡住，卡住就是一直没有回音，干等 30 秒再来两次要 90 秒。所以超时短、
# 多发几次（每次是一条新连接）。
SIMPLE_FETCH_TIMEOUT = (3, 6)
SIMPLE_FETCH_TRIES = 4
CONNECT_SECONDS = 10  # 下载附件时，建立连接（含 TLS 握手）最多等几秒，见 download_pdf


def fetch_page(url: str, wait_selector: str = None, disable_resources: bool = True, solve_cloudflare: bool = False,
               network_idle: bool = True, use_simple_fetch: bool = False):
    """
    抓取单个网页，返回 scrapling 页面对象。打开之前先等这个站点的限速（fetch.transport.wait）。

    参数:
        url:               目标网页 URL
        wait_selector:     等待该 CSS 选择器出现后再返回
        disable_resources: 是否禁止加载图片/字体/CSS（默认 True 提速）
        network_idle:      是否等待网络空闲后再返回（默认 True）
        use_simple_fetch:  True = 不开浏览器，直接发请求（scrapling Fetcher）
                           适合无反爬、无 JS 渲染需求的站点，速度快 10x
    """
    transport.wait(url)
    cookies: list[dict] = []

    if use_simple_fetch:
        page = Fetcher.get(
            url,
            stealthy_headers=True,
            follow_redirects=True,
            timeout=SIMPLE_FETCH_TIMEOUT,
            retries=SIMPLE_FETCH_TRIES,
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
            extra_flags=transport.site_only(url),
        )

    logger.info(f"页面抓取成功: {url} | 状态码: {page.status}")

    # 非 2xx 状态码视为失败，抛出异常，不重试
    if not (200 <= page.status < 300):
        raise RuntimeError(f"HTTP {page.status}，页面请求被拒绝: {url}")

    return {
        "page": page,
        "url": url,
        "uuid": make_uuid(url),
        "timestamp": _now_iso(),
        "cookies": cookies,
    }


def download_pdf(
    pdf_url: str,
    parent_uuid: str = "",
    source_site: str = "",
    cookies: list[dict] | None = None,
    max_bytes: int | None = None,
    timeout: float = 120,
    store_as: str = "",
    proxy: str | None = None,
) -> dict:
    """
    下载远程附件文件到 /data/raw/ 目录（支持 pdf/doc/docx/xls/xlsx）。

    每次调用都重新请求（经 fetch.transport.get：限速、超时重试），成功后按字节摘要复用或保存文件，不覆盖旧原件；
    返回里的 skipped 只表示本地已有相同字节，不表示没检查远端。超过 max_bytes 抛 ValueError("response_too_large")。

    参数:
        cookies: 从抓取页面继承的浏览器 cookies（Playwright 格式），
                 用于需要会话的站点（如 CDE）
        store_as: 原件落盘用的身份（默认是下载网址）。下载链接每次都不同的站点（ChiCTR 的链接带一次性令牌）
                 传附件记录号，同一个文件每次落到同一个路径。
        proxy:   这个站点的代理（fetch.site_crawler.site_proxy，如 nccn 的 DATAFABRIC_PROXY_NCCN）；None 直连。
    返回里的 file_name 是站点在响应头 Content-Disposition 里给的文件名（没有就是空串）。
    """
    file_uuid = make_uuid(pdf_url)
    save_dir = APP.RAW_DIR / source_site
    save_dir.mkdir(parents=True, exist_ok=True)

    headers = dict(DEFAULT_HEADERS)
    headers["Cache-Control"] = "no-cache"
    if cookies:
        headers["Cookie"] = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies if "name" in c
        )

    # 流式读取并设上限（加工层预算）：超过 max_bytes 立即停止，不把超大文件读进内存
    limit = max_bytes if max_bytes is not None else int(os.environ.get(
        "DATAFABRIC_ATTACHMENT_MAX_BYTES", str(100 * 1024 * 1024)))
    # 连接（含 TLS 握手）只等 CONNECT_SECONDS：nccn.org、hkexnews 的个别连接会卡 60 秒以上（实测握手超时 62-76 秒），
    # 干等就把整段读取的时间用光；放弃它，transport.get 换一条连接重试
    with httpx.Client(headers=headers, follow_redirects=True, proxy=proxy) as client:
        resp = transport.get(pdf_url, client=client, cap=limit, timeout=httpx.Timeout(timeout, connect=CONNECT_SECONDS))
    resp.raise_for_status()
    content = resp.content
    if not content:
        raise ValueError("附件响应为空，未采用旧缓存")
    # PDF文件头比可能变化的响应头可靠；其余类型沿用已有判别规则。
    file_ext = ".pdf" if content.startswith(b"%PDF") else _ext_from_response(resp, pdf_url)
    stored = store_original(content, store_as or pdf_url, source_site, file_ext)
    save_path, skipped = stored["local_path"], stored["skipped"]

    logger.info(f"附件已核对远端并保留原件: {pdf_url} → {save_path}")
    return {
        "uuid": file_uuid,
        "local_path": str(save_path),
        "source_url": pdf_url,
        "parent_uuid": parent_uuid,
        "source_site": source_site,
        "file_ext": file_ext,
        "file_name": _file_name_from_response(resp),
        "sha256": stored["sha256"],
        "size": stored["size"],
        "timestamp": _now_iso(),
        "skipped": skipped,
    }

