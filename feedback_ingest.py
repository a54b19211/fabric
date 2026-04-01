"""
==========================================================
  人工审核回灌脚本 —— 读取审核后的 CSV，批量更新 JSON 信封
==========================================================

  使用方法:
    1. 运行 main_etl.py --mode export_review 导出待审核 CSV
    2. 药学人员用 Excel 打开 CSV，填写 manual_score 和 reviewer_note 列
    3. 保存 CSV 后运行本脚本:

       python feedback_ingest.py --csv /data/review/pending_review_20240315.csv

  效果:
    - 根据 CSV 中的 uuid 找到对应的 JSON 信封文件
    - 更新 manual_score、reviewer_note 字段
    - 将 status 从 PENDING_MANUAL_REVIEW 改为 MANUAL_REVIEWED

  注意:
    - 此脚本是独立运行的，不依赖 Prefect 调度
    - 未来兼容 Web 前端审核页面（只需对接相同的 JSON 更新逻辑）
"""

import csv
import json
import argparse
from pathlib import Path

from loguru import logger

from config.settings import APP


def load_review_csv(csv_path: str) -> list:
    """
    读取审核 CSV 文件。

    参数:
        csv_path: CSV 文件路径

    返回:
        list[dict]: 每行数据的字典列表
    """
    records = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(row)
    logger.info(f"读取 CSV 成功: {csv_path} | 共 {len(records)} 条记录")
    return records


def update_json_envelope(record: dict) -> bool:
    """
    根据审核记录更新对应的 JSON 信封文件。

    参数:
        record: CSV 中的一行数据（dict）

    返回:
        bool: 更新成功返回 True
    """
    json_file_path = record.get("json_file_path", "")
    manual_score_str = record.get("manual_score", "").strip()
    reviewer_note = record.get("reviewer_note", "").strip()
    doc_uuid = record.get("uuid", "")

    # ---- 检查 JSON 文件是否存在 ----
    if not json_file_path:
        logger.warning(f"UUID {doc_uuid}: json_file_path 为空，跳过")
        return False

    json_path = Path(json_file_path)
    if not json_path.exists():
        logger.warning(f"UUID {doc_uuid}: JSON 文件不存在: {json_path}")
        return False

    # ---- 检查是否填写了审核打分 ----
    if not manual_score_str:
        logger.debug(f"UUID {doc_uuid}: 未填写审核打分，跳过")
        return False

    # ---- 解析打分 ----
    try:
        manual_score = float(manual_score_str)
    except ValueError:
        logger.warning(f"UUID {doc_uuid}: 审核打分格式错误: '{manual_score_str}'")
        return False

    # ---- 读取并更新 JSON ----
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))

        data["manual_score"] = manual_score
        data["reviewer_note"] = reviewer_note
        data["status"] = "MANUAL_REVIEWED"

        # 如果人工打分较高，更新置信度
        if manual_score >= 0.8:
            data.setdefault("confidence", {})["level"] = "A"
            data["confidence"]["score"] = manual_score
            data["confidence"]["reason"] = f"人工审核确认，打分: {manual_score}"
        elif manual_score >= 0.5:
            data.setdefault("confidence", {})["level"] = "B"
            data["confidence"]["score"] = manual_score
            data["confidence"]["reason"] = f"人工审核确认，打分: {manual_score}"

        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"UUID {doc_uuid}: JSON 更新成功 | 打分: {manual_score}")
        return True

    except Exception as e:
        logger.error(f"UUID {doc_uuid}: 更新 JSON 失败: {e}")
        return False


def run_feedback_ingest(csv_path: str):
    """
    主执行函数：读取 CSV → 逐条更新 JSON。

    参数:
        csv_path: 审核后的 CSV 文件路径
    """
    records = load_review_csv(csv_path)

    success_count = 0
    skip_count = 0
    fail_count = 0

    for record in records:
        result = update_json_envelope(record)
        if result:
            success_count += 1
        elif record.get("manual_score", "").strip():
            fail_count += 1
        else:
            skip_count += 1

    logger.info(
        f"回灌完成 | 成功: {success_count} | 跳过(未填分): {skip_count} | 失败: {fail_count}"
    )


# ===================================================================
#  命令行入口
# ===================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="人工审核回灌脚本 —— 读取 CSV 更新 JSON 信封"
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="审核后的 CSV 文件路径",
    )
    args = parser.parse_args()
    run_feedback_ingest(args.csv)
