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

import concurrent.futures
import re
import time
from loguru import logger
from prefect import task, flow

from config.settings import APP
from config.crawler_settings import SITE_PROFILES
from core_engine.scrapling_client import fetch_page, download_pdf, make_uuid, _sniff_bytes
from core_engine.router import route_webpage, route_pdf
from core_engine.docx_parser import route_docx
from core_engine.excel_parser import route_excel
from core_engine.content_cleaner import clean_content
from core_engine.quality_checker import check_quality, quarantine_data
from core_engine.storage_writer import write_to_database
from core_engine.transform_pipeline import apply_l3_normalization, apply_identifier_alignment
from db.repository import (
    get_document_by_uuid,
)


# ═══════════════════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════════════════

def _sniff_file_type(local_path: str) -> str:
    """通过文件头魔数判断真实类型，返回 .pdf / .xlsx / .docx / .doc / .xls / .bin。"""
    try:
        with open(local_path, "rb") as f:
            ext = _sniff_bytes(f.read())
        return ext
    except Exception as e:
        logger.warning(f"[Crawl] 文件类型嗅探失败 path={local_path}: {e}")
        return ".bin"


def _strip_frontmatter(markdown: str) -> str:
    if not markdown.startswith("---"):
        return markdown
    end = markdown.find("\n---", 3)
    if end == -1:
        return markdown
    return markdown[end + 4:].lstrip("\n")


def _is_transient_read_error(error: Exception) -> bool:
    """Only retry timeouts and throttling; parsing and other HTTP errors are final."""
    seen = set()
    while isinstance(error, BaseException) and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TimeoutError) or any(
            cls.__name__ in {"TimeoutException", "TimeoutError", "ReadTimeout",
                             "ConnectTimeout", "WriteTimeout", "PoolTimeout"}
            for cls in type(error).__mro__
        ):
            return True
        response = getattr(error, "response", None)
        if getattr(response, "status_code", None) == 429:
            return True
        # fetch_page deliberately raises RuntimeError for non-2xx responses.
        if isinstance(error, RuntimeError) and re.search(r"\bHTTP\s+429\b", str(error)):
            return True
        error = error.__cause__ or error.__context__
    return False


def _read_with_retries(read, **kwargs):
    """Bound read retries also for .fn callers, without retrying any database write."""
    retries = max(0, APP.TASK_RETRIES)
    delay = min(max(float(APP.TASK_RETRY_DELAY), 0.0), 60.0)
    for attempt in range(retries + 1):
        try:
            return read(**kwargs)
        except Exception as error:
            if attempt == retries or not _is_transient_read_error(error):
                raise
            logger.warning(
                f"[Crawl] 暂时读取失败，{delay:g}秒后重试 "
                f"({attempt + 1}/{retries}): {type(error).__name__}"
            )
            time.sleep(delay)
            delay = min(delay * 2, 60.0)


def _has_document_text(parsed: dict) -> bool:
    markdown = parsed.get("markdown")
    if not isinstance(markdown, str):
        return False
    text = _strip_frontmatter(markdown).strip()
    # The generic router can return a title even when body extraction failed.
    return bool(text) and text != f"# {parsed.get('title', '')}".strip()


def _validate_parsed_document(parsed, *, allow_attachment_only: bool = False) -> dict:
    """An empty parser result is a failure, not a successful zero-match response."""
    if not isinstance(parsed, dict) or not parsed.get("uuid"):
        raise ValueError("解析器未返回有效文档或文档编号")
    if parsed.get("success") is False or parsed.get("status") == "failed":
        raise ValueError(f"文档解析失败: {parsed.get('error') or parsed.get('uuid')}")
    has_attachments = any(
        isinstance(item, dict) and item.get("url")
        for item in (parsed.get("attachment_links") or [])
    )
    if not _has_document_text(parsed) and not (allow_attachment_only and has_attachments):
        raise ValueError(f"文档正文解析为空: {parsed.get('uuid')}")
    return parsed


# ═══════════════════════════════════════════════════════════════════════
# Prefect Tasks — 每步可被 UI 监控
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="fetch_and_parse_url",
    retries=0,  # Reading retries live below so .fn() has the same bounded policy.
)
def fetch_and_parse_url(url: str, site_key: str, profile: dict, force: bool = False) -> list[dict]:
    """
    抓取单个网页并解析为结构化 dict。
    force保留接口兼容；附件每次核对远端并保留旧文件。
    返回文档列表；抓取/解析失败抛异常，不转换为合法零命中的空列表。
    """
    site_cfg = SITE_PROFILES.get(site_key, {})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        page_data = executor.submit(
            _read_with_retries,
            fetch_page.fn,
            url=url,
            delay=profile.get("request_delay_seconds", 2.0),
            wait_selector=site_cfg.get("detail_wait_selector"),
            disable_resources=site_cfg.get("disable_resources", True),
            solve_cloudflare=site_cfg.get("solve_cloudflare", False),
            use_simple_fetch=site_cfg.get("use_simple_fetch", False),
        ).result()

    parsed = _validate_parsed_document(
        route_webpage.fn(page_data, site_key=site_key), allow_attachment_only=True,
    )
    cookies = page_data.get("cookies", [])
    attachment_links = parsed.get("attachment_links", [])
    merge_attachments = site_cfg.get("merge_attachments", False)

    results = []

    if attachment_links:
        att_results = fetch_and_parse_attachments.fn(
            attachment_links, parsed["uuid"], site_key,
            parent_parsed=parsed, force=force,
            cookies=cookies,
        )
        if not att_results and not _has_document_text(parsed):
            raise ValueError(f"正文为空且没有可解析附件: {parsed.get('uuid')}")
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
    retries=0,  # Reading retries live below so .fn() has the same bounded policy.
)
def fetch_and_parse_attachments(
    attachment_links: list,
    parent_uuid: str,
    site_key: str,
    parent_parsed: dict | None = None,
    force: bool = False,
    cookies: list[dict] | None = None,
) -> list[dict]:
    """下载并解析附件列表。PDF/XLS/XLSX 走 MinerU，doc/docx 走 python-docx。"""
    results = []
    logger.info(f"[Crawl] 共 {len(attachment_links)} 个附件，parent_uuid={parent_uuid}")

    for att in attachment_links:
        att_url = att.get("url", "")
        if not att_url:
            continue

        att_info = _read_with_retries(
            download_pdf.fn, pdf_url=att_url, parent_uuid=parent_uuid,
            source_site=site_key, force=force, cookies=cookies,
        )
        file_ext = (att_info.get("file_ext") or "").lower()
        local_path = att_info.get("local_path", "")

        if file_ext == ".bin" and local_path:
            file_ext = _sniff_file_type(local_path)
            logger.info(f"[Crawl] .bin 嗅探结果: {file_ext}")

        if file_ext in {".xls", ".xlsx"}:
            logger.info(f"[Crawl] → route_excel (直接读取), ext={file_ext}")
            att_info["file_ext"] = file_ext
            results.append(_validate_parsed_document(route_excel(att_info, parent_parsed=parent_parsed)))
        elif file_ext == ".pdf":
            logger.info(f"[Crawl] → MinerU")
            results.append(_validate_parsed_document(route_pdf.fn(att_info)))
        elif file_ext in {".doc", ".docx"}:
            logger.info(f"[Crawl] → route_docx")
            att_info["file_ext"] = file_ext
            results.append(_validate_parsed_document(route_docx(att_info, parent_parsed=parent_parsed)))
        else:
            logger.warning(f"[Crawl] 未知类型 {file_ext}，跳过: {local_path}")

    return results


@task(
    name="process_and_store",
    retries=0,
)
def process_and_store(parsed: dict) -> dict:
    """清洗 → L3归一 → 标识符对齐 → 质检 → 写库，返回写库结果。"""
    cleaned = clean_content.fn(parsed)
    normalized = apply_l3_normalization.fn(cleaned)
    aligned = apply_identifier_alignment.fn(normalized)
    checked = check_quality.fn(aligned)
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
      2. API来源不支持旧URL重采时明确拒绝；不清空旧成果
      3. 网页重新读取后写库须成功；内容变化交版本入口，不覆盖旧资料
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

    # 原成果保留到新资料真正采用；API记录不得把集合URL交给HTML抓取器。
    profile = SITE_PROFILES.get(site_key, {})
    if profile.get("source_type") == "api":
        if log_id:
            update_crawl_log(log_id, "failed", error="api_by_id_recrawl_unsupported")
        return {"success": False, "error": "API记录旧重采入口不支持精确by-ID，请用历史读取入口", "uuid": uuid}
    # 附件每次重新核对远端并保留不可变原件，不覆盖旧文件。
    # 新正文交既有版本发布机制处理；不先清除抽取或图谱结果。
    try:
        parsed_list = fetch_and_parse_url.fn(source_url, site_key, profile, force=False)
    except Exception as e:
        logger.error(f"[Recrawl] 抓取失败: {uuid} | {e}")
        if log_id:
            update_crawl_log(log_id, "failed", total=0, error=str(e))
        return {"success": False, "error": f"抓取失败: {e}", "uuid": uuid}

    # 4. 写库
    stored, failed = 0, 0
    for parsed in parsed_list:
        try:
            result = process_and_store.fn(parsed)
            if not result.get("success", False):
                failed += 1
                continue
            stored += 1
        except Exception as e:
            logger.error(f"[Recrawl] 写库失败: {parsed.get('uuid')} | {e}")
            failed += 1

    if log_id:
        update_crawl_log(log_id, "success" if stored and not failed else "partial" if stored else "failed",
                         total=stored + failed, passed=stored, failed=failed)

    logger.info(f"[Recrawl] 完成: {uuid}，写库 {stored} 条")
    return {"success": bool(stored) and not failed, "uuid": uuid, "stored": stored, "failed": failed}


@flow(name="crawl_url_on_demand", retries=0)
def crawl_url_flow(url: str, site_key: str = "") -> dict:
    """按需获取：明确空结果、解析失败、隔离和未确认保存，不自动重放写入。"""
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
        if not isinstance(parsed_list, list):
            raise ValueError("抓取解析未返回文档列表")
    except Exception as error:
        logger.error(f"[CrawlOnDemand] 抓取/解析失败 url={url} site_key={site_key}: {error}")
        # Record counts describe parsed documents; this stage produced none.
        return {"success": False, "status": "failed", "error_stage": "fetch_parse",
                "error": str(error), "url": url, "total": 0,
                "stored": 0, "failed": 0, "quarantined": 0}

    if not parsed_list:
        return {"success": True, "status": "empty", "url": url, "total": 0,
                "stored": 0, "failed": 0, "quarantined": 0,
                "message": "获取完成，未返回可保存记录"}

    stored, failed, quarantined = 0, 0, 0
    failures = []
    for parsed in parsed_list:
        doc_uuid = parsed.get("uuid", "") if isinstance(parsed, dict) else ""
        try:
            result = process_and_store(parsed)
            if isinstance(result, dict) and result.get("status") == "quarantined":
                quarantined += 1
                failures.append({"uuid": doc_uuid, "status": "quarantined",
                                 "error": "资料未通过质量检查"})
            elif isinstance(result, dict) and result.get("success") is True:
                stored += 1
            else:
                failed += 1
                reason = (result.get("error") or result.get("status")) if isinstance(result, dict) else None
                failures.append({"uuid": doc_uuid, "status": "failed",
                                 "error": str(reason or "保存结果未确认成功；未自动重写")})
        except Exception as error:
            logger.error(f"[CrawlOnDemand] 处理/写库失败: {doc_uuid} | {error}")
            failed += 1
            failures.append({"uuid": doc_uuid, "status": "failed", "error": str(error)})

    status = ("success" if not failed and not quarantined else "partial" if stored
              else "failed" if failed else "quarantined")
    return {"success": not failed and not quarantined, "status": status, "url": url,
            "total": len(parsed_list), "stored": stored, "failed": failed,
            "quarantined": quarantined, "failures": failures}
