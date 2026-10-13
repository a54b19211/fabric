"""
字段级加工管线（T6 血缘埋点载体）。

把 T2（L3 语义归一）与 T3（标准标识符对齐）接入 ETL 主链路，
并在加工前后通过 lineage_tracer 记录 normalize / align 两步血缘，
使「按标准字段反查来源」成为可能（配合 C 组的乱查询 lineage_store）。

设计原则（与 T2/T3/T4 一致）：
  - 异常绝不中断主流程、绝不丢原始值。
  - 归一/对齐未命中时保留原值，仅记录血缘，不改写。
"""

from loguru import logger
from prefect import task

from core_engine.lineage_tracer import snapshot, record, record_step
from biz_rules.cleaners.normalizer_l3 import normalize_l3
from core_engine.authority_client import resolve_identifier

# (容器, 字段名, L3 field_type) —— 需要 L3 归一的字段
_L3_TARGET_FIELDS = [
    ("structured_data", "drug_name", "drug_name"),
    ("structured_data", "dosage", "dosage"),
    ("structured_data", "concentration", "concentration"),
    ("structured_data", "amount", "amount"),
    ("structured_data", "unit", "unit"),
    ("extra", "drug_name", "drug_name"),
    ("extra", "dosage", "dosage"),
    ("extra", "concentration", "concentration"),
]


def _container(parsed_result: dict, name: str) -> dict:
    return parsed_result.setdefault(name, {})


@task(name="apply_l3_normalization")
def apply_l3_normalization(parsed_result: dict) -> dict:
    """
    应用 T2 L3 语义归一（同义词→标准词、单位换算到 g/L），
    并记录 normalize 步血缘。未命中字段保留原值，绝不丢数据。
    """
    before = snapshot(parsed_result)
    for container_name, field, ftype in _L3_TARGET_FIELDS:
        container = _container(parsed_result, container_name)
        val = container.get(field)
        if not isinstance(val, str) or not val.strip():
            continue
        new_val = normalize_l3(val, field_type=ftype)
        if new_val != val:
            record(
                parsed_result,
                field=f"{container_name}.{field}",
                step="normalize",
                input=val,
                output=new_val,
                op="l3_normalize",
            )
            container[field] = new_val
    record_step(parsed_result, step="normalize", before=before, op="l3_normalize")
    return parsed_result


def _extract_drug_name(parsed_result: dict) -> str:
    sd = parsed_result.get("structured_data") or {}
    extra = parsed_result.get("extra") or {}
    return sd.get("drug_name") or extra.get("drug_name") or parsed_result.get("title") or ""


@task(name="apply_identifier_alignment")
def apply_identifier_alignment(parsed_result: dict) -> dict:
    """
    应用 T3 标准标识符对齐（PubChem 等权威库），记录 align 步血缘，
    并把对齐五元组存入 parsed_result["authority_resolutions"]（落库由 C 组负责）。
    无药名 / 解析未命中 / 接口降级时均不丢原始值。
    """
    before = snapshot(parsed_result)
    drug_name = _extract_drug_name(parsed_result)
    resolutions = parsed_result.setdefault("authority_resolutions", {})
    if drug_name:
        try:
            res = resolve_identifier(drug_name)
        except Exception as e:  # 兜底，绝不中断主流程
            logger.warning(f"[Align] 标识符解析异常，跳过对齐: {drug_name} | {e}")
            res = {
                "found": False, "std_id": "", "source_id": "",
                "raw_value": drug_name, "resolved_value": drug_name,
                "confidence": 0.0, "candidates": [], "degraded": True,
            }
        resolutions[drug_name] = res
        record(
            parsed_result,
            field="drug_name",
            step="align",
            input=drug_name,
            output=res.get("std_id") or "",
            op="authority_align",
        )
    record_step(parsed_result, step="align", before=before, op="authority_align")
    return parsed_result
