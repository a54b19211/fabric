import os
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
"""
==========================================================
  ZK_Data Fabric — 数据基座 ETL 主入口
==========================================================

  运行方式:
    python main_etl.py                        # 运行完整流水线（全量续爬）
    python main_etl.py --mode crawl           # 仅采集（网页 + API）
    python main_etl.py --mode local           # 仅处理本地 PDF
    python main_etl.py --mode export_review   # 仅导出待审核 CSV
    python main_etl.py --crawl_mode incremental  # 增量复查：保留旧记录修订，继续读取后续新增
"""

import sys
import argparse
import concurrent.futures
from typing import Optional

from loguru import logger
from prefect import flow, task
from prefect.task_runners import ConcurrentTaskRunner

from config.settings import APP
from config.crawler_settings import SITE_PROFILES, LOCAL_PDF_SCAN_DIRS
from core_engine.scrapling_client import download_pdf, scan_local_pdfs
from core_engine.pagination_driver import make_driver
from core_engine.router import route_pdf
from core_engine.api_router import iter_api_results
from core_engine.storage_writer import write_to_database
from core_engine.review_exporter import export_review_csv
from flows.crawl_flow import (
    _validate_parsed_document,
    fetch_and_parse_url,
    process_and_store,
)
from db.session import check_connection
from migration.migrate import run_migrations


# ======================== 日志配置 ========================
logger.remove()
logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
    level="INFO",
)
logger.add(
    "logs/etl_{time:YYYY-MM-DD}.log",
    rotation="50 MB",
    retention="30 days",
    encoding="utf-8",
    level="DEBUG",
)


# ======================== 确保输出目录存在 ========================
def _ensure_dirs():
    for d in [APP.RAW_DIR, APP.MARKDOWN_DIR, APP.JSON_DIR, APP.QUARANTINE_DIR, APP.REVIEW_DIR, APP.LINEAGE_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ===================================================================
#  公共处理逻辑：质检 → 落盘 / 隔离
# ===================================================================

class CrawlTaskError(RuntimeError):
    """入口失败时保留已完成记录统计；分页错误不冒充一条文档。"""

    def __init__(self, message: str, stats: dict):
        super().__init__(message)
        self.stats = dict(stats)

    def __reduce__(self):
        return type(self), (str(self), self.stats)


def _new_stats() -> dict:
    return {"total": 0, "passed": 0, "quarantined": 0, "failed": 0}


def _merge_stats(stats: dict, other: dict):
    for key in stats:
        stats[key] += other.get(key, 0)


def _crawl_status(stats: dict, error: str = "") -> str:
    if error or stats["failed"]:
        return "partial" if stats["passed"] else "failed"
    return "success"


def _update_task_log(log_id: int, stats: dict, error: str = ""):
    if not log_id:
        return
    from db.repository import update_crawl_log
    update_crawl_log(
        log_id=log_id,
        status=_crawl_status(stats, error),
        total=stats["total"],
        passed=stats["passed"],
        failed=stats["failed"],
        error=error or (f"{stats['failed']} 条记录处理失败" if stats["failed"] else ""),
    )


def _already_processed(doc_uuid: str, site: str) -> bool:
    """
    检查该 UUID 是否已处理，用于断点续爬跳过。
    优先查数据库（DB 可用时），降级查本地 JSON 文件（本地存储模式）。
    """
    # 优先查数据库
    if APP.DB_HOST:
        try:
            from db.repository import get_document_by_uuid
            doc = get_document_by_uuid(doc_uuid)
            if doc is not None:
                return True
        except Exception:
            pass  # DB 查询失败，降级到本地文件
    # 降级：查本地 JSON 文件
    json_path = APP.JSON_DIR / site / f"{doc_uuid}.json"
    return json_path.exists()


def _process_single_result(parsed: dict, stats: dict):
    """保存明确成功才计通过；质量隔离与保存失败分别累计。"""
    try:
        result = process_and_store.fn(parsed)
        if not isinstance(result, dict):
            raise TypeError("处理保存未返回结果对象")
        if result.get("status") == "quarantined":
            stats["quarantined"] += 1
        elif result.get("success") is True:
            stats["passed"] += 1
        else:
            raise RuntimeError(result.get("error") or "保存未确认成功")
    except Exception as e:
        stats["failed"] = stats.get("failed", 0) + 1
        logger.error(f"记录处理失败: {e}")
        return str(e)


# ===================================================================
#  子流程 1：数据采集流水线（网页爬取 + API 拉取）
# ===================================================================

@task(name="crawl_detail_page", retries=0)
def crawl_detail_page(site_key: str, profile: dict, detail_url: str, crawl_mode: str = "full") -> dict:
    """重新读取详情，旧 UUID 也交既有存储机制核对修订；写入不自动重试。"""
    stats = _new_stats()
    try:
        parsed_list = fetch_and_parse_url.fn(detail_url, site_key, profile)
        if not isinstance(parsed_list, list):
            raise TypeError("详情解析未返回结果列表")
    except Exception as e:
        logger.error(f"详情页处理失败: {detail_url} | {e}")
        stats["total"] += 1
        stats["failed"] += 1
        return {"detail_url": detail_url, "stats": stats, "error": str(e)}

    error = ""
    for parsed in parsed_list:
        stats["total"] += 1
        failure = _process_single_result(parsed, stats)
        if failure and not error:
            error = failure

    return {"detail_url": detail_url, "stats": stats, "error": error}


@task(name="crawl_single_start_url", retries=0)
def crawl_single_start_url(site_key: str, profile: dict, start_url: str, crawl_mode: str = "full") -> dict:
    """单入口爬取：本次 URL 去重，不因历史记录停止或跳过修订。"""
    from core_engine.pagination_driver import JsClickDriver
    from db.repository import log_crawl_task

    stats = _new_stats()
    seen_urls = set()
    errors = []  # 数据库日志保存首条具体原因；完整计数和逐条运行日志仍保留。
    log_id = log_crawl_task(
        site_key=site_key,
        task_type=crawl_mode,
        config_snapshot={"start_url": start_url, "max_pages": profile.get("max_pages", 0)},
    )

    def _process_urls(urls: list[str]):
        for detail_url in urls:
            if detail_url in seen_urls:
                continue
            # 本次失败也不立即重写；显式重放时 seen_urls 会重新建立。
            seen_urls.add(detail_url)
            try:
                result = crawl_detail_page.fn(site_key, profile, detail_url, crawl_mode=crawl_mode)
                _merge_stats(stats, result["stats"])
                if result.get("error") and not errors:
                    errors.append(f"{detail_url}: {result['error']}")
            except Exception as e:
                logger.error(f"详情页处理失败: {detail_url} | {e}")
                stats["total"] += 1
                stats["failed"] += 1
                if not errors:
                    errors.append(f"{detail_url}: {e}")

    try:
        driver = make_driver(site_key, profile, start_url)
        if profile.get("source_type") == "table_page":
            for page_batch in driver.iter_table_data():
                for parsed in page_batch:
                    stats["total"] += 1
                    failure = _process_single_result(parsed, stats)
                    if failure and not errors:
                        errors.append(failure)
        elif isinstance(driver, JsClickDriver):
            for page_urls in driver._playwright_crawl():
                _process_urls(page_urls)
        else:
            for detail_url in driver.iter_detail_urls():
                _process_urls([detail_url])
        _update_task_log(log_id, stats, "; ".join(errors))
    except Exception as e:
        error = "; ".join(errors + [str(e)])
        try:
            _update_task_log(log_id, stats, error)
        except Exception as log_error:
            logger.error(f"爬取失败日志更新失败: {log_error}")
        # 不自动重放含写入的批次；向上保留已完成统计和失败原因。
        raise CrawlTaskError(error, stats) from e

    return {"site_key": site_key, "site_name": profile.get("name", site_key), "start_url": start_url,
            "status": _crawl_status(stats), "stats": stats}


@flow(
    name="crawl_websites",
    retries=0,
    task_runner=ConcurrentTaskRunner(max_workers=APP.MAX_CONCURRENCY),
)
def crawl_websites_flow(only_site: Optional[str] = None, crawl_mode: str = "full"):
    """统一采集入口；API 与网页均保留失败和已完成记录统计。"""
    from db.repository import log_crawl_task

    logger.info(f"========== 开始数据采集流水线（{crawl_mode}模式）==========")
    stats = _new_stats()
    failed_sites = {}
    profiles = SITE_PROFILES
    if only_site:
        if only_site not in SITE_PROFILES:
            logger.error(f"站点 '{only_site}' 不存在，可用站点: {list(SITE_PROFILES.keys())}")
            return
        profiles = {only_site: SITE_PROFILES[only_site]}

    # API 读取可合法零命中；读取或分页异常不视为正常结束。
    for site_key, profile in profiles.items():
        if profile.get("source_type") != "api":
            continue
        site_stats = _new_stats()
        site_error = ""
        log_id = log_crawl_task(site_key=site_key, task_type=crawl_mode,
                                config_snapshot={"source_type": "api"})
        try:
            for parsed in iter_api_results(site_key, profile):
                site_stats["total"] += 1
                failure = _process_single_result(parsed, site_stats)
                if failure and not site_error:
                    site_error = failure
            _update_task_log(log_id, site_stats, site_error)
            if site_stats["failed"]:
                failed_sites.setdefault(site_key, []).append("api")
        except Exception as e:
            failed_sites.setdefault(site_key, []).append("api")
            logger.error(f"[API] 站点失败: {profile.get('name', site_key)} | {e}")
            try:
                _update_task_log(log_id, site_stats, "; ".join(x for x in (site_error, str(e)) if x))
            except Exception as log_error:
                logger.error(f"爬取失败日志更新失败: {log_error}")
        _merge_stats(stats, site_stats)

    jobs = []
    for site_key, profile in profiles.items():
        if profile.get("source_type") == "api":
            continue
        for start_url in profile["start_urls"]:
            future = crawl_single_start_url.submit(site_key, profile, start_url, crawl_mode)
            jobs.append({"site_key": site_key, "site_name": profile.get("name", site_key),
                         "start_url": start_url, "future": future})

    for job in jobs:
        try:
            result = job["future"].result()
            s = result["stats"]
            _merge_stats(stats, s)
            if s.get("failed", 0):
                failed_sites.setdefault(job["site_key"], []).append(job["start_url"])
            logger.info(
                f"[Web] 站点入口完成: {result['site_name']} | {result['start_url']} | "
                f"总计={s['total']} 通过={s['passed']} 隔离={s['quarantined']} 失败={s.get('failed', 0)}"
            )
        except Exception as e:
            if isinstance(e, CrawlTaskError):
                _merge_stats(stats, e.stats)
            failed_sites.setdefault(job["site_key"], []).append(job["start_url"])
            logger.error(f"[Web] 站点入口失败: {job['site_name']} | {job['start_url']} | 错误: {e}")

    logger.info(
        f"========== 数据采集完成 | 总计: {stats['total']} | 通过: {stats['passed']} | "
        f"隔离: {stats['quarantined']} | 失败: {stats['failed']} =========="
    )
    for site_key, failed_urls in failed_sites.items():
        logger.error(f"站点失败汇总: {site_key} | 失败入口数: {len(failed_urls)}")
        for url in failed_urls:
            logger.error(f"  - 失败入口: {url}")
    return {"stats": stats, "failed_sites": failed_sites,
            "status": _crawl_status(stats, "采集入口失败" if failed_sites else "")}


# ===================================================================
#  子流程 2：本地 PDF 批量处理流水线
# ===================================================================

@flow(
    name="process_local_pdfs",
    retries=0,
    task_runner=ConcurrentTaskRunner(max_workers=APP.MAX_CONCURRENCY),
)
def process_local_pdfs_flow():
    """批量处理本地 PDF，解析和保存失败均保留到根入口。"""
    logger.info("========== 开始本地 PDF 处理流水线 ==========")
    stats = _new_stats()
    errors = []
    for scan_dir in LOCAL_PDF_SCAN_DIRS:
        try:
            for pdf_info in scan_local_pdfs(scan_dir):
                if _already_processed(pdf_info["uuid"], pdf_info["source_site"]):
                    logger.warning(f"[跳过] 已处理: {pdf_info['local_path']}")
                    continue
                stats["total"] += 1
                try:
                    parsed = _validate_parsed_document(route_pdf.fn(pdf_info))
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"本地 PDF 解析失败: {pdf_info['local_path']} | {e}")
                    if not errors:
                        errors.append(str(e))
                    continue
                failure = _process_single_result(parsed, stats)
                if failure and not errors:
                    errors.append(failure)
        except Exception as e:
            logger.error(f"本地 PDF 扫描失败: {scan_dir} | {e}")
            errors.append(str(e))
    error = "; ".join(errors)
    logger.info(
        f"========== 本地 PDF 处理完成 | 总计: {stats['total']} | 通过: {stats['passed']} | "
        f"隔离: {stats['quarantined']} | 失败: {stats['failed']} =========="
    )
    return {"stats": stats, "status": _crawl_status(stats, error), "error": error}


# ===================================================================
#  主入口 Flow
# ===================================================================

@flow(name="zk_data_fabric_main", retries=0)
def main_pipeline(mode: str = "all", site: Optional[str] = None, crawl_mode: str = "full"):
    """
    总调度入口。

    参数:
        mode:       "all" | "crawl" | "local" | "export_review"
        site:       调试用，指定只跑某个站点，None 则跑所有站点
        crawl_mode: "full"（全量复查）| "incremental"（增量复查，保留旧 ID 修订）
    """
    _ensure_dirs()
    logger.info(
        f"ZK_Data Fabric ETL 启动 | 模式: {mode} | 续爬策略: {crawl_mode}"
        + (f" | 站点: {site}" if site else "")
    )

    # ── 数据库检查 ──
    if not APP.DB_HOST:
        logger.info("[DB] 未配置 DB_HOST，数据仅落本地文件，不写入数据库")
    elif not check_connection():
        logger.warning("[DB] 数据库连接失败，数据仅落本地文件，不写入数据库")
    else:
        logger.info(f"[DB] 连接正常: {APP.DB_HOST}:{APP.DB_PORT}/{APP.DB_NAME}，执行迁移检查")
        run_migrations()

    stats = _new_stats()
    if mode in ("all", "crawl"):
        result = crawl_websites_flow(only_site=site, crawl_mode=crawl_mode)
        if not isinstance(result, dict) or "stats" not in result:
            raise CrawlTaskError("采集未返回明确完成结果", stats)
        _merge_stats(stats, result["stats"])
        if result.get("status") != "success":
            raise CrawlTaskError(f"采集未完整成功: {result.get('failed_sites', {})}", stats)

    if mode in ("all", "local"):
        if LOCAL_PDF_SCAN_DIRS:
            result = process_local_pdfs_flow()
            _merge_stats(stats, result["stats"])
            if result.get("status") != "success":
                raise CrawlTaskError(f"本地 PDF 处理未完整成功: {result.get('error', '')}", stats)
        else:
            logger.info("未配置本地 PDF 扫描目录，跳过")

    if mode in ("all", "export_review"):
        csv_path = export_review_csv()
        if csv_path:
            logger.info(f"待审核 CSV 已导出: {csv_path}")

    logger.info("ZK_Data Fabric ETL 运行结束")
    return {"stats": stats, "status": "success"}


# ===================================================================
#  命令行入口
# ===================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZK_Data Fabric ETL 主入口")
    parser.add_argument(
        "--mode",
        choices=["all", "crawl", "local", "export_review"],
        default="all",
        help="运行模式: all=完整流水线, crawl=仅采集(网页+API), local=仅本地PDF, export_review=仅导出审核CSV",
    )
    parser.add_argument(
        "--site",
        default=None,
        help="调试用：只跑指定站点，如 --site fda_api_enforcement。不传则跑所有站点",
    )
    parser.add_argument(
        "--crawl_mode",
        choices=["full", "incremental"],
        default="full",
        help="续爬策略: full=全量复查, incremental=增量复查；旧记录均核对修订，仅去重本次重复URL",
    )
    args = parser.parse_args()
    main_pipeline(mode=args.mode, site=args.site, crawl_mode=args.crawl_mode)
