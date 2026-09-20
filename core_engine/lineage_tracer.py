"""
字段级血缘追踪器。

记录关键字段（title / drug_name / approval_number / manufacturer）
在 ETL 各加工步骤（parse → clean → normalize → quality）中的输入输出值，
用于定位数据错误的源头。

使用方式：
    from core_engine.lineage_tracer import snapshot, record, locate_first_change

    # 1. 在步骤开始前快照关键字段
    before = snapshot(parsed_result)
    # ... 执行加工 ...
    # 2. 在步骤结束后记录变化
    record(parsed_result, field="title", step="clean",
           input=before["title"], output=parsed_result.get("title", ""), op="clean")

血缘数据结构（parsed_result["lineage"]）：
    {
        "title": [
            {"step": "parse", "op": "css_extract", "input": "", "output": "xxx"},
            {"step": "clean", "op": "clean", "input": "xxx", "output": "xxx"},
        ],
        ...
    }
"""

from typing import Any, Dict, List

# 需要追踪血缘的关键字段
KEY_FIELDS = ("title", "drug_name", "approval_number", "manufacturer")

# 加工步骤顺序（用于排序/定位）
STEP_ORDER = ("parse", "clean", "normalize", "quality")


def _get_field_value(parsed_result: Dict[str, Any], field: str) -> Any:
    """从 parsed_result 中提取指定字段的当前值。

    title 从顶层取，其余字段优先从 structured_data 取，
    取不到再从 extra 兜底。
    """
    if field == "title":
        return parsed_result.get("title", "")
    structured = parsed_result.get("structured_data") or {}
    if field in structured:
        return structured.get(field, "")
    extra = parsed_result.get("extra") or {}
    return extra.get(field, "")


def snapshot(parsed_result: Dict[str, Any]) -> Dict[str, Any]:
    """快照 KEY_FIELDS 的当前值，供步骤结束后对比。"""
    return {field: _get_field_value(parsed_result, field) for field in KEY_FIELDS}


def record(
    parsed_result: Dict[str, Any],
    field: str,
    step: str,
    input: Any,
    output: Any,
    op: str = "",
) -> None:
    """记录某个字段在某一步骤的输入输出值（就地写入 parsed_result["lineage"]）。"""
    lineage: Dict[str, List[Dict[str, Any]]] = parsed_result.setdefault("lineage", {})
    entry = {"step": step, "op": op, "input": input, "output": output}
    lineage.setdefault(field, []).append(entry)


def record_step(
    parsed_result: Dict[str, Any],
    step: str,
    before: Dict[str, Any],
    op: str = "",
) -> None:
    """对 KEY_FIELDS 批量记录某一步骤的输入输出变化。

    before: snapshot() 返回的快照字典。
    """
    for field in KEY_FIELDS:
        record(
            parsed_result,
            field=field,
            step=step,
            input=before.get(field, ""),
            output=_get_field_value(parsed_result, field),
            op=op,
        )


def locate_first_change(lineage: Dict[str, Any], field: str) -> Dict[str, Any] | None:
    """定位某字段第一次发生变化的步骤，返回该条记录 dict，未找到返回 None。"""
    entries = lineage.get(field, []) if lineage else []
    for entry in entries:
        if entry.get("input") != entry.get("output"):
            return entry
    return None
