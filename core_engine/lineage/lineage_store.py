"""
字段级血缘落盘存储。

将 parsed_result["lineage"] 以文档为单位落盘到 data/lineage/<uuid>.json，
并提供从已有 json_envelopes 回填血缘的能力（backfill）。

目录结构：
    data/lineage/
        <site>/
            <uuid1>.json      # 文档1 的字段血缘
            <uuid2>.json      # 文档2 的字段血缘
"""

import json
from typing import Any, Dict

from loguru import logger

from config.settings import APP
from core_engine.lineage.lineage_tracer import snapshot, record_step
from biz_rules.cleaners import normalize_publish_date


def save_lineage(parsed_result: Dict[str, Any]) -> str:
    """把 parsed_result["lineage"] 写入 data/lineage/<site>/<uuid>.json，返回文件路径。"""
    doc_uuid = parsed_result.get("uuid", "")
    if not doc_uuid:
        logger.warning("[Lineage] 缺少 uuid，跳过落盘")
        return ""
    site = parsed_result.get("source_site", "") or "unknown"
    lineage = parsed_result.get("lineage") or {}
    target_dir = APP.LINEAGE_DIR / site
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{doc_uuid}.json"
    path.write_text(
        json.dumps(lineage, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.debug(f"[Lineage] 血缘已落盘: {path}")
    return str(path)


def _rebuild_parsed_result(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """从已落盘的 json_envelope 重建 parsed_result（仅含血缘追踪所需字段）。"""
    provenance = envelope.get("provenance") or {}
    site_key = provenance.get("source_site", "") or ""
    return {
        "uuid": envelope.get("uuid", ""),
        "title": envelope.get("title", ""),
        "publish_date": envelope.get("publish_date", ""),
        "structured_data": {
            "title": envelope.get("title", ""),
            "publish_date": envelope.get("publish_date", ""),
            "category": envelope.get("category", ""),
            "summary": envelope.get("summary", ""),
        },
        "extra": envelope.get("extra") or {},
        "source_site": site_key,
        "parse_method": envelope.get("parse_method", ""),
    }


def _replay_lineage(parsed_result: Dict[str, Any]) -> Dict[str, Any]:
    """按项目流程重放 parse → clean → quality 三步，重建 lineage。"""
    parse_method = parsed_result.get("parse_method", "")

    # parse 步骤（网页/PDF/API 来源均有；parse_method 为空时跳过）
    if parse_method:
        empty_before = {k: "" for k in snapshot(parsed_result)}
        record_step(parsed_result, step="parse", before=empty_before, op=parse_method)

    # clean 步骤（先快照原始值，再执行日期标准化，最后记录）
    before = snapshot(parsed_result)
    normalize_publish_date(parsed_result)
    record_step(parsed_result, step="clean", before=before, op="clean")

    # quality 步骤
    before = snapshot(parsed_result)
    record_step(parsed_result, step="quality", before=before, op="quality_gate:passed")

    return parsed_result.get("lineage", {})


def rebuild_and_save_from_envelope(envelope: Dict[str, Any]) -> str:
    """从单个 json_envelope 重建血缘并落盘到 data/lineage/。"""
    parsed_result = _rebuild_parsed_result(envelope)
    _replay_lineage(parsed_result)
    return save_lineage(parsed_result)


def backfill_all() -> int:
    """扫描 data/json_envelopes/ 下所有信封，重建血缘并落盘，返回处理数量。"""
    json_root = APP.JSON_DIR
    if not json_root.exists():
        logger.warning(f"[Lineage] JSON 目录不存在: {json_root}")
        return 0

    count = 0
    for json_file in sorted(json_root.rglob("*.json")):
        try:
            envelope = json.loads(json_file.read_text(encoding="utf-8"))
            rebuild_and_save_from_envelope(envelope)
            count += 1
        except Exception as e:
            logger.error(f"[Lineage] 回填失败: {json_file} | {e}")
    logger.info(f"[Lineage] 回填完成: 共 {count} 个文档")
    return count
