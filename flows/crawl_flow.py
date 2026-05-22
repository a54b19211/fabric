"""
单文档爬取核心模块

职责：
  - 封装"爬取单个 URL → 解析 → 清洗质检 → 写库"的完整链路
  - 每个步骤都是 Prefect @task，可被 Prefect UI 监控
  - 对外暴露两个 @flow：
      recrawl_document_flow(uuid)   重爬已有文档（API 触发）
      crawl_url_flow(url, site_key) 按需爬新 URL（调试/补录用）
  - main_etl.py 的批量 flow 直接调用本模块的 task，不再内嵌逻辑

调用方式：
  # 在 Prefect 上下文中（被 flow 调用）：直接调用 task
  result = fetch_and_parse_url(url, site_key, profile)

  # 在非 Prefect 上下文中（如 API 线程）：用 .fn() 绕过运行时
  result = fetch_and_parse_url.fn(url, site_key, profile)

  # 提交独立 flow run（API 触发重爬，Prefect UI 可见）：
  recrawl_document_flow(uuid)
"""

import zipfile
import pathlib
import concurrent.futures
from loguru import logger
from prefect import task, flow

from config.settings import APP
from config.crawler_settings import SITE_PROFILES
from core_engine.scrapling_client import fetch_page, download_pdf, make_uuid
from core_engine.router import route_webpage, route_pdf
from core_engine.docx_parser import route_docx
from core_engine.excel_parser import route_excel
from core_engine.content_cleaner import clean_content
from core_engine.quality_checker import check_quality, quarantine_data
from core_engine.storage_writer import write_to_database
from db.repository import (
    get_document_by_uuid,
    reset_document_for_recrawl,
)


# ═══════════════════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════════════════

def _sniff_file_type(local_path: str) -> str:
    """通过文件头魔数判断真实类型，返回 .pdf / .xlsx / .docx / .doc / .xls / .bin。"""
    try:
        with open(local_path, "rb") as f:
            header = f.read(8)
        if header[:4] == b"%PDF":
            return ".pdf"
        if header[:4] == b"\xd0\xcf\x11\xe0":
            ext = pathlib.Path(local_path).suffix.lower()
            return ".xls" if ext == ".xls" else ".doc"
        if header[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(local_path, "r") as zf:
                    names = zf.namelist()
                if any(n.startswith("xl/") for n in names):
                    return ".xlsx"
                if any(n.startswith("word/") for n in names):
                    return ".docx"
            except Exception:
                pass
            ext = pathlib.Path(local_path).suffix.lower()
            if ext in {".xlsx", ".xls"}:
                return ".xlsx"
            return ".docx"
    except Exception:
        pass
    return ".bin"


def _strip_frontmatter(markdown: str) -> str:
    if not markdown.startswith("---"):
        return markdown
    end = markdown.find("\n---", 3)
    if end == -1:
        return markdown
    return markdown[end + 4:].lstrip("\n")


# ═══════════════════════════════════════════════════════════════════════
# Prefect Tasks — 每步可被 UI 监控
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="fetch_and_parse_url",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def fetch_and_parse_url(url: str, site_key: str, profile: dict, force: bool = False) -> dict:
    """
    抓取单个网页并解析为结构化 dict。
    force=True 时附件强制重新下载（重爬场景）。
    """
    site_cfg = SITE_PROFILES.get(site_key, {})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        page_data = executor.submit(
            fetch_page.fn,
            url=url,
            delay=profile.get("request_delay_seconds", 2.0),
            wait_selector=site_cfg.get("detail_wait_selector"),
            disable_resources=site_cfg.get("disable_resources", True),
            solve_cloudflare=site_cfg.get("solve_cloudflare", False),
            use_simple_fetch=site_cfg.get("use_simple_fetch", False),
        ).result()

    parsed = route_webpage.fn(page_data)
    attachment_links = parsed.get("attachment_links", [])
    merge_attachments = site_cfg.get("merge_attachments", False)

    results = []

    if attachment_links:
        att_results = fetch_and_parse_attachments.fn(
            attachment_links, parsed["uuid"], site_key,
            parent_parsed=parsed, force=force,
        )
        if merge_attachments:
            merged_md = parsed.get("markdown", "")
            for att_parsed in att_results:
                att_md = att_parsed.get("markdown", "")
                att_title = att_parsed.get("title", "")
                if att_md:
                    sep = f"\n\n---\n\n## 附件：{att_title}\n\n" if att_title else "\n\n---\n\n"
                    merged_md = merged_md + sep + _strip_frontmatter(att_md)
            parsed["markdown"] = merged_md
            results.append(parsed)
        else:
            results.extend(att_results)
            results.append(parsed)
    else:
        results.append(parsed)

    return results


@task(
    name="fetch_and_parse_attachments",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def fetch_and_parse_attachments(
    attachment_links: list,
    parent_uuid: str,
    site_key: str,
    parent_parsed: dict | None = None,
    force: bool = False,
) -> list[dict]:
    """下载并解析附件列表。PDF/XLS/XLSX 走 MinerU，doc/docx 走 python-docx。"""
    results = []
    logger.info(f"[Crawl] 共 {len(attachment_links)} 个附件，parent_uuid={parent_uuid}")

    for att in attachment_links:
        att_url = att.get("url", "")
        if not att_url:
            continue

        att_info = download_pdf.fn(pdf_url=att_url, parent_uuid=parent_uuid,
                                   source_site=site_key, force=force)
        file_ext = (att_info.get("file_ext") or "").lower()
        local_path = att_info.get("local_path", "")

        if file_ext == ".bin" and local_path:
            file_ext = _sniff_file_type(local_path)
            logger.info(f"[Crawl] .bin 嗅探结果: {file_ext}")

        if file_ext in {".xls", ".xlsx"}:
            logger.info(f"[Crawl] → route_excel (直接读取), ext={file_ext}")
            att_info["file_ext"] = file_ext
            results.append(route_excel(att_info, parent_parsed=parent_parsed))
        elif file_ext == ".pdf":
            logger.info(f"[Crawl] → MinerU")
            results.append(route_pdf.fn(att_info))
        elif file_ext in {".doc", ".docx"}:
            logger.info(f"[Crawl] → route_docx")
            att_info["file_ext"] = file_ext
            results.append(route_docx(att_info, parent_parsed=parent_parsed))
        else:
            logger.warning(f"[Crawl] 未知类型 {file_ext}，跳过: {local_path}")

    return results


@task(
    name="process_and_store",
    retries=0,
)
def process_and_store(parsed: dict) -> dict:
    """清洗 → 质检 → 写库，返回写库结果。"""
    cleaned = clean_content.fn(parsed)
    checked = check_quality.fn(cleaned)
    if checked["quality_passed"]:
        return write_to_database.fn(checked)
    else:
        quarantine_data.fn(checked)
        return {"uuid": parsed.get("uuid", ""), "status": "quarantined", "success": False}


# ═══════════════════════════════════════════════════════════════════════
# Prefect Flows — 可在 Prefect UI 中作为独立 flow run 出现
# ═══════════════════════════════════════════════════════════════════════

@flow(name="recrawl_document")
def recrawl_document_flow(uuid: str) -> dict:
    """
    重爬已有文档（Prefect flow 版本，在 main_etl 批量场景中使用）。
    API 触发时请调用 recrawl_document_fn() 避免 Prefect client 初始化问题。
    """
    return _recrawl_document_impl(uuid)


def recrawl_document_fn(uuid: str) -> dict:
    """
    重爬已有文档（无 Prefect 运行时版本，供 API 后台线程调用）。
    执行结果写入 crawl_log 表，不依赖 Prefect Server。

    流程：
      1. 从数据库取 source_url + source_site
      2. 软删除旧抽取结果，重置 extraction_status / weknora_sync_status
      3. 重新抓取 → 解析 → 清洗质检 → upsert 写库
    """
    return _recrawl_document_impl(uuid)


def _recrawl_document_impl(uuid: str) -> dict:
    """重爬核心逻辑，被 recrawl_document_flow 和 recrawl_document_fn 共用。"""
    from db.repository import log_crawl_task, update_crawl_log

    logger.info(f"[Recrawl] 开始重爬: {uuid}")
    log_id = log_crawl_task(site_key="recrawl", task_type="recrawl",
                            config_snapshot={"uuid": uuid})

    # 1. 取文档信息
    doc = get_document_by_uuid(uuid)
    if doc is None:
        logger.error(f"[Recrawl] 文档不存在: {uuid}")
        if log_id:
            update_crawl_log(log_id, "failed", error="文档不存在")
        return {"success": False, "error": "文档不存在", "uuid": uuid}

    source_url = doc.source_url
    site_key = doc.source_site or ""

    if not source_url:
        logger.error(f"[Recrawl] 文档无 source_url，无法重爬: {uuid}")
        if log_id:
            update_crawl_log(log_id, "failed", error="文档无 source_url")
        return {"success": False, "error": "文档无 source_url", "uuid": uuid}

    # 2. 重置数据库状态
    reset_document_for_recrawl(uuid)
    logger.info(f"[Recrawl] 已重置文档状态: {uuid}")

    # 3. 重新爬取（用 .fn() 绕过 Prefect 运行时，避免 client 初始化）
    profile = SITE_PROFILES.get(site_key, {})
    try:
        parsed_list = fetch_and_parse_url.fn(source_url, site_key, profile, force=True)
    except Exception as e:
        logger.error(f"[Recrawl] 抓取失败: {uuid} | {e}")
        if log_id:
            update_crawl_log(log_id, "failed", total=0, error=str(e))
        return {"success": False, "error": f"抓取失败: {e}", "uuid": uuid}

    # 4. 写库
    stored, failed = 0, 0
    for parsed in parsed_list:
        try:
            process_and_store.fn(parsed)
            stored += 1
        except Exception as e:
            logger.error(f"[Recrawl] 写库失败: {parsed.get('uuid')} | {e}")
            failed += 1

    if log_id:
        update_crawl_log(log_id, "success" if failed == 0 else "partial",
                         total=stored + failed, passed=stored, failed=failed)

    logger.info(f"[Recrawl] 完成: {uuid}，写库 {stored} 条")
    return {"success": True, "uuid": uuid, "stored": stored}


@flow(name="crawl_url_on_demand")
def crawl_url_flow(url: str, site_key: str = "") -> dict:
    """
    按需爬取一个新 URL（调试/补录用）。
    site_key 为空时自动从 URL 匹配。
    """
    if not site_key:
        url_lower = url.lower()
        for key, profile in SITE_PROFILES.items():
            pattern = profile.get("url_pattern", "")
            if pattern and pattern in url_lower:
                site_key = key
                break

    profile = SITE_PROFILES.get(site_key, {})
    logger.info(f"[CrawlOnDemand] url={url} site_key={site_key}")

    try:
        parsed_list = fetch_and_parse_url(url, site_key, profile)
    except Exception as e:
        return {"success": False, "error": str(e), "url": url}

    stored = 0
    for parsed in parsed_list:
        try:
            process_and_store(parsed)
            stored += 1
        except Exception as e:
            logger.error(f"[CrawlOnDemand] 写库失败: {e}")
    return {"success": True, "url": url, "stored": stored}
