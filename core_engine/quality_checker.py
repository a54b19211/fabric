"""
数据质量门控 —— Quality Gate（多维质量评分，论文主创新）。

将"只看篇幅"的布尔闸门升级为 5 维度打分 → 加权总分 → 高/中/低/隔离 四级路由。
对应开题报告 §R19「前期探索性原型暴露的不足」之"质量判据仅 markdown 长度阈值"。
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

from loguru import logger
from prefect import task

from config.settings import APP
from core_engine.lineage_tracer import snapshot, record_step


# ======================== 多维质量评分配置 ========================
# 五维度权重（Σ = 1.0）——主创新"多维质量评分"的聚合口径
_DIM_WEIGHTS = {
    "completeness": 0.25,   # 完整性：字段/正文是否齐全
    "accuracy": 0.20,       # 准确性：抽取结果是否可信
    "timeliness": 0.15,     # 时效性：发布时间是否新鲜
    "authority": 0.25,      # 权威性：来源可信度（复用 T3 的 calculate_confidence）
    "consistency": 0.15,    # 一致性：正文与结构化字段是否自洽
}

# 四级路由阈值（总分 0~1）
_LEVEL_HIGH = 0.80     # 总分 ≥ 0.80 → 高
_LEVEL_MEDIUM = 0.60   # 总分 ≥ 0.60 → 中
_LEVEL_LOW = 0.40      # 总分 ≥ 0.40 → 低；< 0.40 → 隔离


def _route_level(total: float) -> str:
    """加权总分 → 等级。"""
    if total >= _LEVEL_HIGH:
        return "高"
    if total >= _LEVEL_MEDIUM:
        return "中"
    if total >= _LEVEL_LOW:
        return "低"
    return "隔离"


def _score_completeness(p: dict) -> float:
    score = 0.0
    md = p.get("markdown", "") or ""
    if len(md) >= APP.MIN_MARKDOWN_LENGTH:
        score += 0.5
    sd = p.get("structured_data") or {}
    if isinstance(sd, dict) and len(sd) > 0:
        score += 0.3
    if p.get("title"):
        score += 0.2
    return min(score, 1.0)


def _score_accuracy(p: dict) -> float:
    score = 0.0
    md = p.get("markdown", "") or ""
    if len(md) >= APP.MIN_MARKDOWN_LENGTH:
        score += 0.5
    sd = p.get("structured_data") or {}
    if isinstance(sd, dict) and len(sd) > 0:
        score += 0.3
    # 标识符对齐命中（T3 产物）作为准确性信号。
    # 注意：T3/T6 实际写入的是 authority_resolutions（复数，按药名索引的 dict），
    # 单数 authority_resolution 仅作兼容保留。
    ars = p.get("authority_resolutions")
    if isinstance(ars, dict) and ars:
        aligned = any(
            isinstance(v, dict) and bool(v.get("resolved_value"))
            for v in ars.values()
        )
    else:
        ar = p.get("authority_resolution") or {}
        aligned = bool(isinstance(ar, dict) and ar.get("resolved_value"))
    if aligned:
        score += 0.2
    return min(score, 1.0)


def _score_timeliness(p: dict) -> float:
    pd = p.get("publish_date") or (p.get("metadata") or {}).get("publish_date")
    if not pd:
        return 0.5  # 无时间信息，给中性分，不贸然判劣
    try:
        year = int(str(pd)[:4])
    except (ValueError, TypeError):
        return 0.5
    if year < 1900 or year > datetime.now().year:
        return 0.5
    age = datetime.now().year - year
    if age <= 5:
        return 1.0
    if age <= 10:
        return 0.7
    if age <= 20:
        return 0.5
    return 0.3


def _score_authority(p: dict) -> float:
    """权威性：复用 T3 修复后的来源置信度（DOI/期刊/官方站点）。

    DOI / 期刊名在真实 ETL 中由 router 写入 parsed_result["sniffed_meta"]
    （storage_writer 计算置信度时亦从此取值），故优先读 sniffed_meta /
    metadata，顶层同名字段仅作兜底，避免权威性维度恒为无信号基线 0.3。
    """
    try:
        from biz_rules.source_mapper import calculate_confidence

        meta = p.get("sniffed_meta") or p.get("metadata") or {}
        r = calculate_confidence(
            source_url=p.get("source_url") or "",
            doi=p.get("doi") or meta.get("doi"),
            journal_name=p.get("journal_name") or meta.get("journal_name"),
        )
        return float(r.get("score", 0.3))
    except Exception:
        # 异常绝不影响主流程，给最低可信基线
        return 0.3


def _score_consistency(p: dict) -> float:
    sd = p.get("structured_data") or {}
    if not isinstance(sd, dict) or len(sd) == 0:
        return 0.8  # 无结构化字段，默认基线自洽
    nonempty = [v for v in sd.values() if v not in (None, "", [])]
    if len(nonempty) == len(sd):
        return 1.0
    return 0.5


def _score_dimensions(p: dict) -> dict:
    return {
        "completeness": _score_completeness(p),
        "accuracy": _score_accuracy(p),
        "timeliness": _score_timeliness(p),
        "authority": _score_authority(p),
        "consistency": _score_consistency(p),
    }


def _collect_issues(p: dict, dims: dict, level: str) -> list:
    issues = []
    md = p.get("markdown", "") or ""
    if len(md) < APP.MIN_MARKDOWN_LENGTH:
        issues.append(f"Markdown 过短({len(md)}字符 < {APP.MIN_MARKDOWN_LENGTH})")
    if not (p.get("structured_data") or {}):
        issues.append("缺少结构化字段(structured_data 为空)")
    if not p.get("title"):
        issues.append("缺少标题(title)")
    if dims.get("authority", 0.3) <= 0.3:
        issues.append("来源权威性低(无 DOI/期刊/已知官方站点)")
    if level == "隔离":
        issues.append("综合质量分低于隔离阈值，转入隔离区复核")
    return issues


@task(name="check_quality")
def check_quality(parsed_result: dict) -> dict:
    """
    数据质量检查（多维质量评分，论文主创新）。

    计算 5 维度分(完整性/准确性/时效性/权威性/一致性) → 加权聚合总分 →
    映射到 高/中/低/隔离 四级路由。

    参数:
        parsed_result: router.route_webpage() 或 route_pdf() 的返回值

    返回:
        dict: 原 parsed_result 追加字段:
        {
            "quality_score": float,        # 加权总分 0~1
            "quality_level": str,          # 高/中/低/隔离
            "quality_dimensions": dict,     # 各维度 0~1
            "quality_passed": bool,        # 非"隔离"即通过
            "quality_issues": list,
        }
    """
    before = snapshot(parsed_result)
    dims = _score_dimensions(parsed_result)
    total = sum(_DIM_WEIGHTS[d] * dims[d] for d in dims)
    total = max(0.0, min(1.0, total))
    level = _route_level(total)
    issues = _collect_issues(parsed_result, dims, level)

    parsed_result["quality_score"] = round(total, 4)
    parsed_result["quality_level"] = level
    parsed_result["quality_dimensions"] = {k: round(v, 4) for k, v in dims.items()}
    parsed_result["quality_passed"] = (level != "隔离")
    parsed_result["quality_issues"] = issues

    record_step(
        parsed_result,
        step="quality",
        before=before,
        op=f"quality_gate:{level}",
    )

    if level == "隔离":
        logger.warning(
            f"[Quality Gate] 数据隔离: {parsed_result['uuid']} | "
            f"总分={total:.3f} | 问题: {'; '.join(issues)}"
        )
    else:
        logger.info(
            f"[Quality Gate] 质检通过(等级={level}): {parsed_result['uuid']} | 总分={total:.3f}"
        )

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
        "quality_score": parsed_result.get("quality_score"),
        "quality_level": parsed_result.get("quality_level"),
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
