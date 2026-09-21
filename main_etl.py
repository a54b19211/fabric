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
    python main_etl.py --crawl_mode incremental  # 增量爬取：遇到第一个重复即停止
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

class StopIncremental(Exception):
    """增量模式下遇到已处理记录时抛出，用于中断翻页循环。"""


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
    """对单条解析结果执行清洗、质检和落盘。"""
    result = process_and_store.fn(parsed)
    if result.get("success", True) and result.get("status") != "quarantined":
        stats["passed"] += 1
    else:
        stats["quarantined"] += 1


# ===================================================================
#  子流程 1：数据采集流水线（网页爬取 + API 拉取）
# ===================================================================

@task(
    name="crawl_detail_page",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def crawl_detail_page(site_key: str, profile: dict, detail_url: str, crawl_mode: str = "full") -> dict:
    """爬取单条详情页并完成解析 → 质检 → 落盘。"""
    stats = {"total": 0, "passed": 0, "quarantined": 0}

    # ── 断点续爬：全量=跳过已处理，增量=遇到重复立即停止 ──
    from core_engine.scrapling_client import make_uuid as _make_uuid
    doc_uuid = _make_uuid(detail_url)
    if _already_processed(doc_uuid, site_key):
        if crawl_mode == "incremental":
            logger.info(f"[增量] 遇到已处理记录，停止: {detail_url}")
            raise StopIncremental(detail_url)
        logger.info(f"[跳过] 已处理: {detail_url}")
        return {"detail_url": detail_url, "stats": stats}

    try:
        parsed_list = fetch_and_parse_url.fn(detail_url, site_key, profile)
    except Exception as e:
        logger.error(f"详情页处理失败: {detail_url} | {e}")
        stats["total"] += 1
        return {"detail_url": detail_url, "stats": stats}

    for parsed in parsed_list:
        stats["total"] += 1
        _process_single_result(parsed, stats)

    return {"detail_url": detail_url, "stats": stats}




@task(
    name="crawl_single_start_url",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def crawl_single_start_url(site_key: str, profile: dict, start_url: str, crawl_mode: str = "full") -> dict:
    """单个站点入口的完整爬取任务：翻页驱动 → 详情页处理。"""
    from core_engine.pagination_driver import JsClickDriver
    from db.repository import log_crawl_task, update_crawl_log

    stats = {"total": 0, "passed": 0, "quarantined": 0}
    stop_flag = {"stop": False}

    # ── 爬取日志：任务开始 ──
    log_id = log_crawl_task(
        site_key=site_key,
        task_type=crawl_mode,
        config_snapshot={"start_url": start_url, "max_pages": profile.get("max_pages", 0)},
    )

    def _process_urls(urls: list[str]):
        for detail_url in urls:
            try:
                result = crawl_detail_page.fn(site_key, profile, detail_url, crawl_mode=crawl_mode)
                s = result["stats"]
                stats["total"] += s["total"]
                stats["passed"] += s["passed"]
                stats["quarantined"] += s["quarantined"]
            except StopIncremental:
                stop_flag["stop"] = True
                return  # 停止当前页剩余 URL 的处理
            except Exception as e:
                logger.error(f"详情页处理失败: {detail_url} | {e}")
                stats["total"] += 1

    try:
        # ── 表格直提模式：直接从列表页提取表格内容，不经过详情页 ──
        if profile.get("source_type") == "table_page":
            driver = make_driver(site_key, profile, start_url)
            for page_batch in driver.iter_table_data():
                for parsed in page_batch:
                    stats["total"] += 1
                    _process_single_result(parsed, stats)
        else:
            driver = make_driver(site_key, profile, start_url)
            if isinstance(driver, JsClickDriver):
                for page_urls in driver._playwright_crawl():
                    _process_urls(page_urls)
                    if stop_flag["stop"]:
                        logger.info(f"[增量] 站点 {site_key} 翻页已停止")
                        break
            else:
                for detail_url in driver.iter_detail_urls():
                    _process_urls([detail_url])
                    if stop_flag["stop"]:
                        logger.info(f"[增量] 站点 {site_key} 翻页已停止")
                        break

        # ── 爬取日志：任务成功 ──
        if log_id:
            update_crawl_log(
                log_id=log_id,
                status="success",
                total=stats["total"],
                passed=stats["passed"],
                failed=stats["total"] - stats["passed"] - stats["quarantined"],
            )
    except Exception as e:
        # ── 爬取日志：任务失败 ──
        if log_id:
            update_crawl_log(
                log_id=log_id,
                status="failed",
                total=stats["total"],
                passed=stats["passed"],
                failed=stats["total"] - stats["passed"] - stats["quarantined"],
                error=str(e),
            )
        raise

    return {"site_key": site_key, "site_name": profile.get("name", site_key), "start_url": start_url, "stats": stats}


@flow(
    name="crawl_websites",
    task_runner=ConcurrentTaskRunner(max_workers=APP.MAX_CONCURRENCY),
)
def crawl_websites_flow(only_site: Optional[str] = None, crawl_mode: str = "full"):
    """
    统一数据采集入口。根据站点 source_type 自动分发：
    - source_type='api'：API 拉取
    - 其他（默认）：网页爬取
    """
    logger.info(f"========== 开始数据采集流水线（{crawl_mode}模式）==========")
    stats = {"total": 0, "passed": 0, "quarantined": 0}

    profiles = SITE_PROFILES
    if only_site:
        if only_site not in SITE_PROFILES:
            logger.error(f"站点 '{only_site}' 不存在，可用站点: {list(SITE_PROFILES.keys())}")
            return
        profiles = {only_site: SITE_PROFILES[only_site]}

    # ---- API 来源站点（串行，受限于 API 速率）----
    for site_key, profile in profiles.items():
        if profile.get("source_type") != "api":
            continue
        logger.info(f"[API] 站点已入队: {profile['name']} ({site_key})")
        try:
            for parsed in iter_api_results(site_key, profile):
                stats["total"] += 1
                _process_single_result(parsed, stats)
            logger.info(f"[API] 站点完成: {profile['name']}")
        except Exception as e:
            logger.error(f"[API] 站点失败: {profile['name']} | {e}")

    # ---- 网页爬取站点（Prefect 并发）----
    jobs = []
    for site_key, profile in profiles.items():
        if profile.get("source_type") == "api":
            continue
        logger.info(f"[Web] 站点已入队: {profile['name']} ({site_key})")
        for start_url in profile["start_urls"]:
            future = crawl_single_start_url.submit(site_key, profile, start_url, crawl_mode)
            jobs.append({"site_key": site_key, "site_name": profile["name"], "start_url": start_url, "future": future})

    failed_sites = {}
    for job in jobs:
        try:
            result = job["future"].result()
            s = result["stats"]
            stats["total"] += s["total"]
            stats["passed"] += s["passed"]
            stats["quarantined"] += s["quarantined"]
            logger.info(
                f"[Web] 站点入口完成: {result['site_name']} | {result['start_url']} | "
                f"总计={s['total']} 通过={s['passed']} 隔离={s['quarantined']}"
            )
        except Exception as e:
            failed_sites.setdefault(job["site_key"], []).append(job["start_url"])
            logger.error(f"[Web] 站点入口失败: {job['site_name']} | {job['start_url']} | 错误: {e}")

    logger.info(
        f"========== 数据采集完成 | "
        f"总计: {stats['total']} | 通过: {stats['passed']} | 隔离: {stats['quarantined']} =========="
    )
    if failed_sites:
        for site_key, failed_urls in failed_sites.items():
            logger.error(f"站点失败汇总: {site_key} | 失败入口数: {len(failed_urls)}")
            for url in failed_urls:
                logger.error(f"  - 失败入口: {url}")


# ===================================================================
#  子流程 2：本地 PDF 批量处理流水线
# ===================================================================

@flow(
    name="process_local_pdfs",
    task_runner=ConcurrentTaskRunner(max_workers=APP.MAX_CONCURRENCY),
)
def process_local_pdfs_flow():
    """批量扫描本地目录中的 PDF → MinerU 解析 → 质检 → 落盘。"""
    logger.info("========== 开始本地 PDF 处理流水线 ==========")
    stats = {"total": 0, "passed": 0, "quarantined": 0}

    for scan_dir in LOCAL_PDF_SCAN_DIRS:
        for pdf_info in scan_local_pdfs(scan_dir):
            if _already_processed(pdf_info["uuid"], pdf_info["source_site"]):
                logger.warning(f"[跳过] 已处理: {pdf_info['local_path']}")
                continue
            parsed = route_pdf.fn(pdf_info)
            stats["total"] += 1
            _process_single_result(parsed, stats)

    logger.info(
        f"========== 本地 PDF 处理完成 | "
        f"总计: {stats['total']} | 通过: {stats['passed']} | 隔离: {stats['quarantined']} =========="
    )


# ===================================================================
#  主入口 Flow
# ===================================================================

@flow(name="zk_data_fabric_main")
def main_pipeline(mode: str = "all", site: Optional[str] = None, crawl_mode: str = "full"):
    """
    总调度入口。

    参数:
        mode:       "all" | "crawl" | "local" | "export_review"
        site:       调试用，指定只跑某个站点，None 则跑所有站点
        crawl_mode: "full"（全量续爬）| "incremental"（增量，遇到重复即停）
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

    if mode in ("all", "crawl"):
        crawl_websites_flow(only_site=site, crawl_mode=crawl_mode)

    if mode in ("all", "local"):
        if LOCAL_PDF_SCAN_DIRS:
            process_local_pdfs_flow()
        else:
            logger.info("未配置本地 PDF 扫描目录，跳过")

    if mode in ("all", "export_review"):
        csv_path = export_review_csv()
        if csv_path:
            logger.info(f"待审核 CSV 已导出: {csv_path}")

    logger.info("ZK_Data Fabric ETL 运行结束")


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
        help="续爬策略: full=全量续爬（跳过已处理，继续爬剩余）, incremental=增量（遇到第一个重复即停止）",
    )
    args = parser.parse_args()
    main_pipeline(mode=args.mode, site=args.site, crawl_mode=args.crawl_mode)
