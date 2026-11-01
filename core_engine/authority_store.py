"""
T3 标准标识符对齐结果的旁路存档。

背景：对齐结果（parsed_result["authority_resolutions"]）自 transform_pipeline 产生后，
既不在 documents 表，也不在 doc_extra 表，质检之外无人可见。落库正解是新建
doc_authority_resolution 表（见 task_doc/B2_标识符对齐落库设计.md），但该迁移需与
服务器上的 V017–V019 版本底座合并后才能执行，故先采用「旁路存档」过渡：

    data/authority/
        <site>/
            <uuid>.json      # 一份文档的全部对齐结果 + 汇总统计

与 data/lineage/ 同构，不改任何表结构、不新增迁移，因此与服务器未同步的
10 个提交零冲突。将来建表后可直接用这些文件回填。

文件结构：
    {
      "uuid": "...",
      "source_site": "...",
      "summary": {"total": 2, "matched": 1, "match_rate": 0.5},
      "resolutions": {"阿司匹林": {...对齐五元组...}}
    }
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from config.settings import APP

# 目录固定在 data/authority，不占用 settings.py 的字段，避免与服务器改动重叠
AUTHORITY_DIRNAME = "authority"


def authority_dir() -> Path:
    """返回对齐结果的根目录（data/authority）。"""
    data_root = Path(getattr(APP, "DATA_ROOT", Path("data")))
    return data_root / AUTHORITY_DIRNAME


def summarize(resolutions: Dict[str, Any]) -> Dict[str, Any]:
    """统计一份文档的对齐命中情况。

    matched 的判定：found 为真且 std_id 非空。降级（degraded）且未命中的不计入命中。

    skipped（占位名：安慰剂/盐水/空白对照）不计入命中率分母 —— 它们本来就不该
    送权威库，若算作「未命中」会系统性低估对齐能力。eligible = total - skipped。
    """
    resolutions = resolutions or {}
    total = len(resolutions)
    matched = 0
    skipped = 0
    for res in resolutions.values():
        if not isinstance(res, dict):
            continue
        if res.get("skipped"):
            skipped += 1
            continue
        if res.get("found") and str(res.get("std_id") or "").strip():
            matched += 1
    eligible = total - skipped
    return {
        "total": total,
        "eligible": eligible,
        "skipped": skipped,
        "matched": matched,
        "match_rate": round(matched / eligible, 4) if eligible else 0.0,
    }


def save_authority_resolutions(parsed_result: Dict[str, Any]) -> str:
    """把对齐结果写入 data/authority/<site>/<uuid>.json，返回文件路径；无结果时跳过。"""
    doc_uuid = parsed_result.get("uuid", "")
    if not doc_uuid:
        logger.warning("[Authority] 缺少 uuid，跳过对齐结果存档")
        return ""

    resolutions = parsed_result.get("authority_resolutions") or {}
    id_resolutions = parsed_result.get("identifier_resolutions") or {}
    if not resolutions and not id_resolutions:
        # 既无药名也无标识符（如未接入对齐的来源）时不产生空文件，避免污染统计
        return ""

    site = parsed_result.get("source_site", "") or "unknown"
    payload = {
        "uuid": doc_uuid,
        "source_site": site,
        # 药名 → 权威库（在线）与标识符 → 格式校验（本地）分开统计，口径不同
        "summary": summarize(resolutions),
        "resolutions": resolutions,
        "identifier_summary": summarize(id_resolutions),
        "identifier_resolutions": id_resolutions,
    }
    target_dir = authority_dir() / site
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{doc_uuid}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug(f"[Authority] 对齐结果已存档: {path}")
    return str(path)


def load_authority_resolutions(doc_uuid: str, site: str) -> Optional[Dict[str, Any]]:
    """读取某文档的对齐结果存档；不存在时返回 None。"""
    path = authority_dir() / (site or "unknown") / f"{doc_uuid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # 存档损坏不应影响调用方
        logger.warning(f"[Authority] 读取失败: {path} | {e}")
        return None
