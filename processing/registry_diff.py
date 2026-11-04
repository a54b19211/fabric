"""临床登记的确定性差分：同一登记号前后两个固定版本，逐字段比较已投影的登记字段。

产出的是可核对的观测——“登记状态从 RECRUITING 改为 TERMINATED”，带两版的版本号和 JSON 路径。
它不判断临床成败或疗效：登记状态改变不等于药物失败，日期推迟不等于方案出问题。

material 只用于雷达分诊路由（哪些变化值得交给模型判断），不是事实结论。规则随处理器版本固定，
改规则必须升 PROCESSOR_VERSION，旧结果保留。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

PROCESSOR_VERSION = "datafabric.registry_diff.v1"

# 登记字段 → 变化类型。trial_id 是身份本身，不比较。
FIELDS: dict[str, str] = {
    "status": "status_change",
    "why_stopped": "why_stopped_change",
    "phase": "phase_change",
    "study_type": "design_change",
    "design_info": "design_change",
    "enrollment": "enrollment_change",
    "enrollment_type": "enrollment_type_change",
    "start_date": "date_change",
    "start_date_type": "date_type_change",
    "primary_completion_date": "date_change",
    "primary_completion_date_type": "date_type_change",
    "completion_date": "date_change",
    "completion_date_type": "date_type_change",
    "conditions": "condition_change",
    "interventions": "intervention_change",
    "arm_groups": "arm_change",
    "primary_outcomes": "primary_outcome_change",
    "secondary_outcomes": "secondary_outcome_change",
    "eligibility": "eligibility_change",
    "locations": "location_change",
    "lead_sponsor": "sponsor_change",
    "collaborators": "collaborator_change",
    "official_title": "title_change",
    "organization_study_id": "identifier_change",
    "secondary_ids": "identifier_change",
    "has_results": "results_flag_change",
    "results_first_posted": "results_posted",
    "results_first_posted_type": "results_posted",
    "results_participant_flow": "results_data_change",
    "results_baseline": "results_data_change",
    "results_outcome_measures": "results_data_change",
    "results_adverse_events": "results_data_change",
}

# 值超过这个长度只存哈希和规模，完整值在两个固定版本里
INLINE_CHARS = 2000
DATE_SHIFT_MATERIAL_DAYS = 90
ENROLLMENT_MATERIAL_RATIO = 0.10

MISSING = {"present": False}


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stored(facts: dict, key: str):
    """存储用的值：缺字段记 present=false（不当成 null 或 0）；长值只存哈希和规模。"""
    if key not in facts:
        return MISSING
    value = facts[key]
    text = _canonical(value)
    if len(text) <= INLINE_CHARS:
        return {"present": True, "value": value}
    out = {"present": True, "sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text)}
    if isinstance(value, list):
        out["count"] = len(value)
    return out


def _parse_date(value):
    """CT 日期是 YYYY-MM-DD 或 YYYY-MM。返回 (date, 精度)；不认识的返回 (None, None)。"""
    if not isinstance(value, str):
        return None, None
    parts = value.split("-")
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2])), "day"
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1), "month"
    except ValueError:
        return None, None
    return None, None


def _names(items) -> list[str]:
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("measure") or item.get("label") or item.get("id")
            if name:
                out.append(str(name))
    return out


def _detail(key: str, old, new) -> tuple[dict, bool, str | None]:
    """(细节, 是否值得交给模型判断, 理由)。old/new 是原值（缺字段为 None）。"""
    kind = FIELDS[key]
    if kind in {"status_change", "why_stopped_change", "phase_change", "sponsor_change"}:
        return {}, True, kind
    if kind == "results_posted" or kind == "results_data_change":
        return {}, True, kind
    if kind == "results_flag_change":
        return {}, new is True, "results_now_posted" if new is True else None
    if kind == "enrollment_change":
        if isinstance(old, int) and isinstance(new, int) and not isinstance(old, bool):
            delta = new - old
            ratio = abs(delta) / old if old else None
            material = ratio is None or ratio >= ENROLLMENT_MATERIAL_RATIO
            return {"delta": delta, "ratio": round(ratio, 4) if ratio is not None else None}, material, (
                "enrollment_shift" if material else None
            )
        return {}, True, "enrollment_appeared_or_removed"
    if kind == "enrollment_type_change":
        return {}, new == "ACTUAL", "enrollment_now_actual" if new == "ACTUAL" else None
    if kind == "date_change":
        a, pa = _parse_date(old)
        b, pb = _parse_date(new)
        if a and b:
            days = (b - a).days
            precision = "day" if pa == pb == "day" else "month"
            direction = "later" if days > 0 else "earlier" if days < 0 else "same_at_precision"
            material = key != "start_date" and abs(days) >= DATE_SHIFT_MATERIAL_DAYS
            return {"days": days, "precision": precision, "direction": direction}, material, (
                "date_shift_" + direction if material else None
            )
        return {}, key != "start_date", "date_appeared_or_removed" if key != "start_date" else None
    if kind == "date_type_change":
        # ESTIMATED → ACTUAL：计划日期变成实际发生
        material = new == "ACTUAL" and key != "start_date_type"
        return {}, material, "date_now_actual" if material else None
    if kind in {"condition_change", "intervention_change", "arm_change", "primary_outcome_change"}:
        before, after = _names(old), _names(new)
        added = [x for x in after if x not in before]
        removed = [x for x in before if x not in after]
        detail = {"added": added[:20], "removed": removed[:20]}
        # 只改了描述措辞（名称集合不变）的方案修订，不当成值得判断的变化；主要终点除外
        material = bool(added or removed) or kind == "primary_outcome_change"
        return detail, material, kind if material else None
    if kind == "identifier_change":
        return {}, True, "identifier_link_change"  # 新挂上的 CTR/ChiCTR/EudraCT 号是身份候选线索
    if kind == "location_change":
        before = len(old) if isinstance(old, list) else 0
        after = len(new) if isinstance(new, list) else 0
        return {"count_before": before, "count_after": after}, False, None
    return {}, False, None


def diff(old_facts: dict, new_facts: dict, locators: dict | None = None) -> list[dict]:
    """两版登记字段的逐字段差异。两边相同（含都缺）的字段不出现。"""
    locators = locators or {}
    changes = []
    for key, kind in FIELDS.items():
        a_present, b_present = key in old_facts, key in new_facts
        a, b = old_facts.get(key), new_facts.get(key)
        if a_present == b_present and _canonical(a) == _canonical(b):
            continue
        detail, material, reason = _detail(key, a, b)
        changes.append(
            {
                "field": key,
                "change_kind": kind,
                "json_pointer": (locators.get(key) or {}).get("json_pointer"),
                "old_value": _stored(old_facts, key),
                "new_value": _stored(new_facts, key),
                "detail": detail,
                "material": bool(material),
                "material_reason": reason,
            }
        )
    return changes


def change_id(from_version_id: str, to_version_id: str, field: str) -> str:
    return hashlib.sha256(
        _canonical([PROCESSOR_VERSION, from_version_id, to_version_id, field]).encode()
    ).hexdigest()


def describe(change: dict) -> str:
    """一行中文说明，给雷达和报告用；只复述两版的值，不加推断。"""
    def show(stored):
        if not stored.get("present"):
            return "（原件无此字段）"
        if "value" in stored:
            text = _canonical(stored["value"])
            return text if len(text) <= 200 else text[:200] + "…"
        return f"（长字段，{stored.get('chars')} 字符）"

    head = f"{change['field']}（{change.get('json_pointer') or '?'}）: {show(change['old_value'])} → {show(change['new_value'])}"
    d = change.get("detail") or {}
    if "days" in d:
        head += f"；相差 {d['days']} 天（{d['precision']} 精度）"
    if "delta" in d:
        head += f"；变化 {d['delta']:+d}"
    if d.get("added") or d.get("removed"):
        head += f"；新增 {d.get('added')}，移除 {d.get('removed')}"
    if "count_before" in d:
        head += f"；地点数 {d['count_before']} → {d['count_after']}"
    return head
