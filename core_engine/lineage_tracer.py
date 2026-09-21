"""
字段级血缘追踪器。

按站点追踪「该站点实际抽取的字段」在 ETL 各加工步骤
（parse → clean → quality）中的输入输出值，用于定位数据错误的源头。

追踪字段范围：
  - 通用字段：title / publish_date / category / summary
  - 站点特有字段：由 biz_rules.schemas.EXTRA_MODEL_MAP 中该站点的 Extra 模型字段决定；
    站点无 Extra 模型时，回退到该文档 extra 中实际出现的键。

使用方式：
    from core_engine.lineage_tracer import snapshot, record, record_step, locate_first_change

    # 1. 在步骤开始前快照关键字段
    before = snapshot(parsed_result)
    # ... 执行加工 ...
    # 2. 在步骤结束后记录变化
    record_step(parsed_result, step="clean", before=before, op="clean")

血缘数据结构（parsed_result["lineage"]）：
    {
        "title": [
            {"step": "parse", "op": "css_extract", "input": "", "output": "xxx"},
            {"step": "clean", "op": "clean", "input": "xxx", "output": "xxx"},
        ],
        ...
    }
"""

from typing import Any, Dict, List, Sequence, Tuple

from biz_rules.schemas import EXTRA_MODEL_MAP

# 所有站点都需要追踪的通用字段
STANDARD_FIELDS: Tuple[str, ...] = ("title", "publish_date", "category", "summary")

# 加工步骤顺序（用于排序/定位）
STEP_ORDER: Tuple[str, ...] = ("parse", "clean", "normalize", "quality")


def get_tracked_fields(
    site_key: str = "",
    observed_extra_keys: Sequence[str] = (),
) -> Tuple[str, ...]:
    """返回某站点需要追踪的字段列表（通用字段在前，站点特有字段在后，去重保序）。"""
    model_cls = EXTRA_MODEL_MAP.get(site_key)
    if model_cls is not None:
        extra_fields: List[str] = list(model_cls.model_fields.keys())
    else:
        extra_fields = [k for k in observed_extra_keys if k not in STANDARD_FIELDS]

    result: List[str] = []
    seen = set()
    for field in list(STANDARD_FIELDS) + extra_fields:
        if field in seen:
            continue
        seen.add(field)
        result.append(field)
    return tuple(result)


def _get_field_value(parsed_result: Dict[str, Any], field: str) -> Any:
    """从 parsed_result 中提取指定字段的当前值。

    title 从顶层取，其余字段优先从 structured_data 取，
    取不到再从 extra 兜底。null 值统一归一化为空字符串。
    """
    if field == "title":
        value = parsed_result.get("title", "")
    else:
        structured = parsed_result.get("structured_data") or {}
        if field in structured:
            value = structured.get(field, "")
        else:
            extra = parsed_result.get("extra") or {}
            value = extra.get(field, "")
    return value if value is not None else ""


def snapshot(parsed_result: Dict[str, Any]) -> Dict[str, Any]:
    """快照该站点需要追踪字段的当前值，供步骤结束后对比。"""
    site_key = parsed_result.get("source_site", "") or ""
    observed = (parsed_result.get("extra") or {}).keys()
    fields = get_tracked_fields(site_key, observed)
    return {field: _get_field_value(parsed_result, field) for field in fields}


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
    """对该站点需要追踪的字段批量记录某一步骤的输入输出变化。

    before: snapshot() 返回的快照字典。
    """
    site_key = parsed_result.get("source_site", "") or ""
    observed = (parsed_result.get("extra") or {}).keys()
    for field in get_tracked_fields(site_key, observed):
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
