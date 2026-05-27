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



@task(name="check_quality")
def check_quality(parsed_result: dict) -> dict:
    """
    数据质量检查。判定数据是否达标，不达标则标记为隔离。

    检查项:
    1. Markdown 文本长度 ≥ MIN_MARKDOWN_LENGTH

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
