import os
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
"""
==========================================================
  ZK_Data Fabric — 数据基座 ETL 主入口
==========================================================

  运行方式:
    python main_etl.py                        # 运行完整流水线
    python main_etl.py --mode crawl           # 仅爬取网页
    python main_etl.py --mode local           # 仅处理本地 PDF
    python main_etl.py --mode export_review   # 仅导出待审核 CSV
"""

import sys
import argparse

from loguru import logger
from prefect import flow, task
from prefect.task_runners import ConcurrentTaskRunner

from config.settings import APP
from config.crawler_settings import SITE_PROFILES, LOCAL_PDF_SCAN_DIRS
from core_engine.scrapling_client import fetch_page, download_pdf, scan_local_pdfs
from core_engine.router import route_webpage, route_pdf
from core_engine.quality_checker import check_quality, quarantine_data
from core_engine.storage_writer import write_three_in_one
from core_engine.review_exporter import export_review_csv


# ======================== 日志配置 ========================
# 移除默认 handler，统一用 loguru 格式
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
    """创建所有需要的输出目录。"""
    for d in [APP.RAW_DIR, APP.MARKDOWN_DIR, APP.JSON_DIR, APP.QUARANTINE_DIR, APP.REVIEW_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ===================================================================
#  子流程 1：网页采集流水线
# ===================================================================

@task(
    name="crawl_single_start_url",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def crawl_single_start_url(site_key: str, profile: dict, start_url: str) -> dict:
    """
    单个站点入口 URL 的完整处理任务（Prefect 队列最小执行单元）。

    说明：
    - Prefect 在这个粒度做并发与失败监控（哪个站点/URL失败一目了然）
    - 站点内部逻辑（正文解析、附件下载）仍在代码内串行执行，便于维护
    """
    stats = {"total": 0, "passed": 0, "quarantined": 0}

    # 1) 抓取页面（调用 task.fn，避免 task 内再套 task）
    page_data = fetch_page.fn(
        url=start_url,
        headers=profile.get("headers"),
        delay=profile.get("request_delay_seconds", 2.0),
    )

    # 2) 路由解析正文
    parsed = route_webpage.fn(page_data)
    stats["total"] += 1

    # 3) 处理附件：支持下载 pdf/doc/docx；当前仅 PDF 进入解析
    for att in parsed.get("attachment_links", []):
        att_url = att.get("url", "")
        if not att_url:
            continue

        if att_url.startswith("/"):
            from urllib.parse import urljoin
            att_url = urljoin(start_url, att_url)

        att_info = download_pdf.fn(
            pdf_url=att_url,
            parent_uuid=parsed["uuid"],
            source_site=site_key,
        )

        file_ext = (att_info.get("file_ext") or "").lower()
        if file_ext == ".pdf":
            pdf_parsed = route_pdf.fn(att_info)
            _process_single_result(pdf_parsed, stats)
            stats["total"] += 1
        elif file_ext in {".doc", ".docx"}:
            logger.info(
                f"附件已下载但暂不解析（仅保留原始文件）: {att_info.get('local_path', '')}"
            )
        else:
            logger.warning(
                f"附件类型不在当前处理范围，已仅保存原始文件: {att_info.get('local_path', '')}"
            )

    # 4) 主文档质检和落盘
    _process_single_result(parsed, stats)

    return {
        "site_key": site_key,
        "site_name": profile.get("name", site_key),
        "start_url": start_url,
        "stats": stats,
    }


@flow(
    name="crawl_websites",
    task_runner=ConcurrentTaskRunner(max_workers=APP.MAX_CONCURRENCY),
)
def crawl_websites_flow():
    """
    爬取所有配置站点的网页，走路由解析 → 质检 → 落盘。
    """
    logger.info("========== 开始网页采集流水线 ==========")
    stats = {"total": 0, "passed": 0, "quarantined": 0}

    jobs = []
    for site_key, profile in SITE_PROFILES.items():
        logger.info(f"站点已入队: {profile['name']} ({site_key})")
        for start_url in profile["start_urls"]:
            future = crawl_single_start_url.submit(site_key, profile, start_url)
            jobs.append(
                {
                    "site_key": site_key,
                    "site_name": profile["name"],
                    "start_url": start_url,
                    "future": future,
                }
            )

    failed_sites = {}
    for job in jobs:
        try:
            result = job["future"].result()
            sub_stats = result["stats"]
            stats["total"] += sub_stats["total"]
            stats["passed"] += sub_stats["passed"]
            stats["quarantined"] += sub_stats["quarantined"]
            logger.info(
                f"站点入口处理完成: {result['site_name']} | {result['start_url']} | "
                f"总计={sub_stats['total']} 通过={sub_stats['passed']} 隔离={sub_stats['quarantined']}"
            )
        except Exception as e:
            key = job["site_key"]
            failed_sites.setdefault(key, []).append(job["start_url"])
            logger.error(
                f"站点入口处理失败: {job['site_name']} | {job['start_url']} | 错误: {e}"
            )

    logger.info(
        f"========== 网页采集完成 | "
        f"总计: {stats['total']} | 通过: {stats['passed']} | "
        f"隔离: {stats['quarantined']} =========="
    )

    if failed_sites:
        for site_key, failed_urls in failed_sites.items():
            logger.error(f"站点失败汇总: {site_key} | 失败入口数: {len(failed_urls)}")
            for failed_url in failed_urls:
                logger.error(f"  - 失败入口: {failed_url}")


# ===================================================================
#  子流程 2：本地 PDF 批量处理流水线
# ===================================================================

@flow(
    name="process_local_pdfs",
    task_runner=ConcurrentTaskRunner(max_workers=APP.MAX_CONCURRENCY),
)
def process_local_pdfs_flow():
    """
    批量扫描本地目录中的 PDF → MinerU 解析 → 质检 → 落盘。
    """
    logger.info("========== 开始本地 PDF 处理流水线 ==========")
    stats = {"total": 0, "passed": 0, "quarantined": 0}

    for scan_dir in LOCAL_PDF_SCAN_DIRS:
        pdf_list = scan_local_pdfs(scan_dir)

        for pdf_info in pdf_list:
            parsed = route_pdf(pdf_info)
            stats["total"] += 1
            _process_single_result(parsed, stats)

    logger.info(
        f"========== 本地 PDF 处理完成 | "
        f"总计: {stats['total']} | 通过: {stats['passed']} | "
        f"隔离: {stats['quarantined']} =========="
    )


# ===================================================================
#  公共处理逻辑：质检 → 落盘 / 隔离
# ===================================================================

def _process_single_result(parsed: dict, stats: dict):
    """对单条解析结果执行质检和落盘。"""
    checked = check_quality(parsed)

    if checked["quality_passed"]:
        write_three_in_one(checked)
        stats["passed"] += 1
    else:
        quarantine_data(checked)
        stats["quarantined"] += 1


# ===================================================================
#  主入口 Flow
# ===================================================================

@flow(name="zk_data_fabric_main")
def main_pipeline(mode: str = "all"):
    """
    总调度入口。

    参数:
        mode: 运行模式
            - "all": 完整流水线（爬取 + 本地 PDF + 导出审核）
            - "crawl": 仅爬取网页
            - "local": 仅处理本地 PDF
            - "export_review": 仅导出待审核 CSV
    """
    _ensure_dirs()
    logger.info(f"ZK_Data Fabric ETL 启动 | 模式: {mode}")

    if mode in ("all", "crawl"):
        crawl_websites_flow()

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
        help="运行模式: all=完整流水线, crawl=仅爬取, local=仅本地PDF, export_review=仅导出审核CSV",
    )
    args = parser.parse_args()
    main_pipeline(mode=args.mode)
