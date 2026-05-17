"""
审核 CSV 导出器 —— 将所有 PENDING_MANUAL_REVIEW 状态的 JSON 汇总导出为 CSV。
供药学人员在 Excel 中人工审核打分。
"""

import csv
import json
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger
from prefect import task

from config.settings import APP


@task(name="export_review_csv")
def export_review_csv() -> str:
    """
    扫描 /data/json_envelopes/ 下所有 JSON 文件，
    筛选出 status == "PENDING_MANUAL_REVIEW" 的记录，导出为 CSV。

    CSV 列包含：UUID、标题、来源、置信度等级、分数、DOI、期刊名、审核打分（空列）、备注（空列）。
    药学人员在 Excel 中填写"审核打分"和"备注"列后，用 feedback_ingest.py 回灌。

    返回:
        str: 导出的 CSV 文件路径
    """
    json_root = APP.JSON_DIR
    if not json_root.exists():
        logger.warning(f"JSON 目录不存在: {json_root}")
        return ""

    pending_records = []

    # 递归扫描所有 JSON 文件
    for json_file in sorted(json_root.rglob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if data.get("status") == "PENDING_REVIEW":
                confidence = data.get("confidence", {})
                provenance = data.get("provenance", {})
                pending_records.append({
                    "uuid": data.get("uuid", ""),
                    "title": data.get("title", ""),
                    "source_url": provenance.get("source_url", ""),
                    "local_file_path": provenance.get("local_file_path", ""),
                    "confidence_score": confidence.get("score", ""),
                    "confidence_reason": confidence.get("reason", ""),
                    "json_file_path": str(json_file),
                    # 以下两列留空，供人工填写
                    "manual_score": "",
                    "reviewer_note": "",
                })
        except Exception as e:
            logger.error(f"读取 JSON 失败: {json_file} | {e}")

    if not pending_records:
        logger.info("没有待审核的记录")
        return ""

    # 导出 CSV
    review_dir = APP.REVIEW_DIR
    review_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = review_dir / f"pending_review_{timestamp}.csv"

    fieldnames = list(pending_records[0].keys())
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(pending_records)

    logger.info(f"[Review] 导出待审核 CSV: {csv_path} | 共 {len(pending_records)} 条")
    return str(csv_path)
