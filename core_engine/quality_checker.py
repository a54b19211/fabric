"""
数据质量门控 —— Quality Gate。
解析失败、Markdown 过短、结构化严重缺失的数据 → 隔离区 /data/quarantine/。
"""

import json
import shutil
from pathlib import Path

from loguru import logger
from prefect import task

from config.settings import APP


def _count_filled_fields(data: dict) -> float:
    """计算顶层解析结果中关键字段的填充率。"""
    if not data:
        return 0.0
    # 从顶层字段取值，兼容特解（css_extract）和通解（trafilatura+qwen）两种路径
    key_fields = ["title", "publish_date", "summary"]
    # 顶层有 title/publish_date，structured_data 里有 category/summary/drug_name
    structured = data.get("structured_data", {}) or {}
    merged = {**structured, "title": data.get("title", ""), "publish_date": data.get("publish_date", "")}
    filled = sum(1 for k in key_fields if merged.get(k))
    return filled / len(key_fields)


@task(name="check_quality")
def check_quality(parsed_result: dict) -> dict:
    """
    数据质量检查。判定数据是否达标，不达标则标记为隔离。

    检查项:
    1. Markdown 文本长度 ≥ MIN_MARKDOWN_LENGTH
    2. 结构化字段填充率 ≥ MIN_JSON_FILLED_RATIO

    参数:
        parsed_result: router.route_webpage() 或 route_pdf() 的返回值

    返回:
        dict: 原 parsed_result 追加以下字段:
        {
            "quality_passed": True/False,
            "quality_issues": ["issue1", "issue2"],
        }
    """
    issues = []
    markdown = parsed_result.get("markdown", "")
    structured = parsed_result.get("structured_data", {})

    # ---- 检查 1: Markdown 长度 ----
    if len(markdown) < APP.MIN_MARKDOWN_LENGTH:
        issues.append(
            f"Markdown 过短({len(markdown)}字符 < {APP.MIN_MARKDOWN_LENGTH})"
        )

    # ---- 检查 2: 结构化字段填充率 ----
    fill_ratio = _count_filled_fields(parsed_result)
    # PDF 解析时 Qwen 可能未配置，structured_data 为空属正常情况，只要 markdown 有内容即可
    parse_method = parsed_result.get("parse_method", "")
    is_pdf_parse = "mineru" in parse_method or "win32com" in parse_method
    effective_threshold = 0.0 if is_pdf_parse else APP.MIN_JSON_FILLED_RATIO
    if fill_ratio < effective_threshold:
        issues.append(
            f"结构化字段填充率过低({fill_ratio:.0%} < {APP.MIN_JSON_FILLED_RATIO:.0%})"
        )

    passed = len(issues) == 0
    parsed_result["quality_passed"] = passed
    parsed_result["quality_issues"] = issues

    if not passed:
        logger.warning(
            f"[Quality Gate] 数据未通过质检: {parsed_result['uuid']} | "
            f"问题: {'; '.join(issues)}"
        )
    else:
        logger.info(f"[Quality Gate] 质检通过: {parsed_result['uuid']}")

    return parsed_result


@task(name="quarantine_data")
def quarantine_data(parsed_result: dict) -> str:
    """
    将未通过质检的数据转入隔离区。

    在 /data/quarantine/ 下创建以 UUID 命名的子目录，保存:
    - quarantine_info.json: 隔离原因和原始解析结果摘要
    - content.md: 原始 Markdown（如有）

    参数:
        parsed_result: 经 check_quality 标记后的解析结果

    返回:
        str: 隔离目录路径
    """
    doc_uuid = parsed_result["uuid"]
    q_dir = APP.QUARANTINE_DIR / doc_uuid
    q_dir.mkdir(parents=True, exist_ok=True)

    # 保存隔离信息
    info = {
        "uuid": doc_uuid,
        "source_url": parsed_result.get("source_url", ""),
        "parse_method": parsed_result.get("parse_method", ""),
        "quality_issues": parsed_result.get("quality_issues", []),
        "markdown_length": len(parsed_result.get("markdown", "")),
        "structured_data": parsed_result.get("structured_data", {}),
    }
    info_path = q_dir / "quarantine_info.json"
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    # 保存 Markdown（即使很短也保存，方便人工查看）
    md_content = parsed_result.get("markdown", "")
    if md_content:
        md_path = q_dir / "content.md"
        md_path.write_text(md_content, encoding="utf-8")

    logger.info(f"[Quarantine] 数据已隔离: {q_dir}")
    return str(q_dir)
