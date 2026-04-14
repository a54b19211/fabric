import os
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
"""
==========================================================
  ZK_Data Fabric — 数据基座 ETL 主入口
==========================================================

  运行方式:
    python main_etl.py                        # 运行完整流水线
    python main_etl.py --mode crawl           # 仅采集（网页 + API）
    python main_etl.py --mode local           # 仅处理本地 PDF
    python main_etl.py --mode export_review   # 仅导出待审核 CSV
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
from core_engine.scrapling_client import fetch_page, download_pdf, scan_local_pdfs
from core_engine.pagination_driver import make_driver
from core_engine.router import route_webpage, route_pdf
from core_engine.api_router import iter_api_results
from core_engine.quality_checker import check_quality, quarantine_data
from core_engine.storage_writer import write_three_in_one
from core_engine.review_exporter import export_review_csv
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
    for d in [APP.RAW_DIR, APP.MARKDOWN_DIR, APP.JSON_DIR, APP.QUARANTINE_DIR, APP.REVIEW_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ===================================================================
#  公共处理逻辑：质检 → 落盘 / 隔离
# ===================================================================

def _process_single_result(parsed: dict, stats: dict):
    """对单条解析结果执行质检和落盘。"""
    checked = check_quality.fn(parsed)
    if checked["quality_passed"]:
        write_three_in_one.fn(checked)
        stats["passed"] += 1
    else:
        quarantine_data.fn(checked)
        stats["quarantined"] += 1


# ===================================================================
#  子流程 1：数据采集流水线（网页爬取 + API 拉取）
# ===================================================================

@task(
    name="crawl_detail_page",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def crawl_detail_page(site_key: str, profile: dict, detail_url: str) -> dict:
    """爬取单条详情页并完成解析 → 质检 → 落盘。"""
    stats = {"total": 0, "passed": 0, "quarantined": 0}
    site_cfg = SITE_PROFILES.get(site_key, {})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        page_data = executor.submit(
            fetch_page.fn,
            url=detail_url,
            delay=profile.get("request_delay_seconds", 2.0),
            wait_selector=site_cfg.get("detail_wait_selector"),
            disable_resources=site_cfg.get("disable_resources", True),
            solve_cloudflare=site_cfg.get("solve_cloudflare", False),
            use_simple_fetch=site_cfg.get("use_simple_fetch", False),
        ).result()

    parsed = route_webpage.fn(page_data)
    stats["total"] += 1

    for att_parsed in route_pdf_attachments(parsed.get("attachment_links", []), parsed["uuid"], site_key):
        _process_single_result(att_parsed, stats)
        stats["total"] += 1

    _process_single_result(parsed, stats)
    return {"detail_url": detail_url, "stats": stats}


def route_pdf_attachments(attachment_links: list, parent_uuid: str, site_key: str) -> list[dict]:
    """下载并解析详情页的 PDF 附件。doc/docx 只下载不解析。"""
    results = []
    for att in attachment_links:
        att_url = att.get("url", "")
        if not att_url:
            continue
        att_info = download_pdf.fn(pdf_url=att_url, parent_uuid=parent_uuid, source_site=site_key)
        file_ext = (att_info.get("file_ext") or "").lower()
        if file_ext == ".pdf":
            results.append(route_pdf.fn(att_info))
        elif file_ext in {".doc", ".docx"}:
            logger.info(f"附件已下载但暂不解析: {att_info.get('local_path', '')}")
    return results


@task(
    name="crawl_single_start_url",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def crawl_single_start_url(site_key: str, profile: dict, start_url: str) -> dict:
    """单个站点入口的完整爬取任务：翻页驱动 → 详情页处理。"""
    from core_engine.pagination_driver import JsClickDriver

    stats = {"total": 0, "passed": 0, "quarantined": 0}

    def _process_urls(urls: list[str]):
        for detail_url in urls:
            try:
                result = crawl_detail_page.fn(site_key, profile, detail_url)
                s = result["stats"]
                stats["total"] += s["total"]
                stats["passed"] += s["passed"]
                stats["quarantined"] += s["quarantined"]
            except Exception as e:
                logger.error(f"详情页处理失败: {detail_url} | {e}")

    driver = make_driver(site_key, profile, start_url)
    if isinstance(driver, JsClickDriver):
        driver.run_with_callback(_process_urls)
    else:
        for detail_url in driver.iter_detail_urls():
            _process_urls([detail_url])

    return {"site_key": site_key, "site_name": profile.get("name", site_key), "start_url": start_url, "stats": stats}


@flow(
    name="crawl_websites",
    task_runner=ConcurrentTaskRunner(max_workers=APP.MAX_CONCURRENCY),
)
def crawl_websites_flow(only_site: Optional[str] = None):
    """
    统一数据采集入口。根据站点 source_type 自动分发：
    - source_type='api'：API 拉取
    - 其他（默认）：网页爬取
    """
    logger.info("========== 开始数据采集流水线 ==========")
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
            future = crawl_single_start_url.submit(site_key, profile, start_url)
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
            parsed = route_pdf(pdf_info)
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
def main_pipeline(mode: str = "all", site: Optional[str] = None):
    """
    总调度入口。

    参数:
        mode: "all" | "crawl" | "local" | "export_review"
        site: 调试用，指定只跑某个站点，None 则跑所有站点
    """
    _ensure_dirs()
    logger.info(f"ZK_Data Fabric ETL 启动 | 模式: {mode}" + (f" | 站点: {site}" if site else ""))

    # ── 数据库检查 ──
    if not APP.DB_HOST:
        logger.info("[DB] 未配置 DB_HOST，数据仅落本地文件，不写入数据库")
    elif not check_connection():
        logger.warning("[DB] 数据库连接失败，数据仅落本地文件，不写入数据库")
    else:
        logger.info(f"[DB] 连接正常: {APP.DB_HOST}:{APP.DB_PORT}/{APP.DB_NAME}，执行迁移检查")
        run_migrations()

    if mode in ("all", "crawl"):
        crawl_websites_flow(only_site=site)

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
    args = parser.parse_args()
    main_pipeline(mode=args.mode, site=args.site)
