"""临床登记的确定性差分：同一登记号前后两个固定版本，比较已投影的登记字段、简介和原件其余部分。

产出的是可核对的观测——“登记状态从 RECRUITING 改为 TERMINATED”，带两版的完整版本号和 JSON 路径。
它不判断临床成败或疗效：登记状态改变不等于药物失败，日期推迟不等于方案出问题。

分诊路由：只有能证明不影响判断的变化才不送模型（不是列出值得判断的变化）。
等价白名单只限确实可等价的字段——正式题目只差大小写/空白；名称类列表只差顺序
（ORDER_ONLY_FIELDS，不改大小写）；地点只差联系人。结果、终点、单位、描述等一律按原值比较
（mL→ML 也是变化）。投影外剩余原件的变化单独判断，不因同时有别的小修订而被遮住。
- 白名单（material=false）只有上面三种。
- 其余任何变化——包括设计、入组标准、同名分组的描述（如剂量）、日期的任何移动——都 material=true。
- 投影字段之外的变化也不放过：简介（briefSummary）变了记 description_change；原件（去掉来源每次
  改写的数据日期）还有别处变了、但上面都没捕捉到，记 unmapped_change，都送模型。
- 旧值缺失（present=false）就是未知，描述里写“原件无此字段”，不换算成 0。
规则随处理器版本固定，改规则必须升 PROCESSOR_VERSION，旧结果保留。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

PROCESSOR_VERSION = "datafabric.registry_diff.v3"

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
SUMMARY_POINTER = "/protocolSection/descriptionModule/briefSummary"
SITE_CONTACT_KEYS = {"contacts"}

# 值超过这个长度只存哈希和规模，完整值在两个固定版本里
INLINE_CHARS = 2000

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


# 只差顺序就等价的字段：都是名称/编号集合，顺序没有含义。不改大小写——单位、剂量写法都可能有意义。
ORDER_ONLY_FIELDS = {"conditions", "collaborators", "secondary_ids", "interventions", "arm_groups", "phase"}


def _spacing_and_case(value):
    return re.sub(r"\s+", " ", value).strip().casefold() if isinstance(value, str) else value


def _ordered(value):
    return sorted(value, key=_canonical) if isinstance(value, list) else value


def _without_contacts(sites):
    if not isinstance(sites, list):
        return sites
    return [{k: v for k, v in s.items() if k not in SITE_CONTACT_KEYS} if isinstance(s, dict) else s for s in sites]


def immaterial_reason(key, old, new, old_present, new_present) -> str | None:
    """能证明不影响判断时给出理由；否则 None（要送模型）。字段出现或消失一律不算。"""
    if not (old_present and new_present):
        return None
    if key == "official_title" and _spacing_and_case(old) == _spacing_and_case(new):
        return "title_case_or_spacing_only"
    if key in ORDER_ONLY_FIELDS and _canonical(_ordered(old)) == _canonical(_ordered(new)):
        return "order_only"
    if key == "locations" and _canonical(_without_contacts(old)) == _canonical(_without_contacts(new)):
        return "site_contacts_only"
    return None


def _detail(key, old, new, old_present, new_present) -> dict:
    """描述用的细节（天数、增减、名称增删、地点数），只复述，不参与“是否送模型”。缺失就是未知。"""
    kind = FIELDS[key]
    if kind == "enrollment_change" and isinstance(old, int) and isinstance(new, int):
        return {"delta": new - old}
    if kind == "date_change":
        return _date_detail(old, new)
    if kind in {"condition_change", "intervention_change", "arm_change", "primary_outcome_change",
                "secondary_outcome_change"}:
        return _names_detail(old, new)
    if kind == "location_change":
        return {"count_before": len(old) if old_present and isinstance(old, list) else None,
                "count_after": len(new) if new_present and isinstance(new, list) else None}
    return {}


def _date_detail(old, new) -> dict:
    a, pa = _parse_date(old)
    b, pb = _parse_date(new)
    if not (a and b):
        return {}
    days = (b - a).days
    return {
        "days": days,
        "precision": "day" if pa == pb == "day" else "month",
        "direction": "later" if days > 0 else "earlier" if days < 0 else "same_at_precision",
    }


def _names_detail(old, new) -> dict:
    before, after = _names(old), _names(new)
    detail = {"added": [x for x in after if x not in before][:20],
              "removed": [x for x in before if x not in after][:20]}
    if not detail["added"] and not detail["removed"]:
        detail["same_names_content_changed"] = True  # 同名项内容变了（如剂量、描述），要看原件
    return detail


def covered() -> tuple[str, ...]:
    """已投影比较过的原件路径：登记字段的 JSON 路径 + 简介。剩余部分另行比较。"""
    from fetch.sources import TRIAL_PATHS

    return tuple(TRIAL_PATHS.values()) + (SUMMARY_POINTER,) + BOOKKEEPING


# 登记簿记日期：每次更新都会变，记录的是“发生了一次更新”本身，不是内容；不当成投影外的内容变化。
BOOKKEEPING = (
    "/protocolSection/statusModule/lastUpdatePostDateStruct",
    "/protocolSection/statusModule/lastUpdateSubmitDate",
    "/protocolSection/statusModule/statusVerifiedDate",
)


def remainder(raw_stable, pointers=None):
    """去掉已覆盖路径后的原件（规范 JSON）。原件不是 JSON 时返回 None（不比较，交给上层按原件哈希）。"""
    if raw_stable is None:
        return None
    try:
        data = json.loads(raw_stable)
    except ValueError:
        return None
    for pointer in pointers if pointers is not None else covered():
        parts = [p for p in pointer.split("/") if p]
        node = data
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and parts:
            node.pop(parts[-1], None)
    return _canonical(data)


def _changed_paths(old_text, new_text, limit=10):
    """剩余原件里变了的顶层以下两级路径，帮读者定位；只是线索，不是完整清单。"""
    old, new = json.loads(old_text), json.loads(new_text)
    out = []

    def walk(a, b, path, depth):
        if len(out) >= limit or _canonical(a) == _canonical(b):
            return
        if depth < 3 and isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                walk(a.get(key), b.get(key), path + "/" + key, depth + 1)
        else:
            out.append(path or "/")

    walk(old, new, "", 0)
    return out


def diff(old_facts: dict, new_facts: dict, locators: dict | None = None, *, old_summary=None, new_summary=None,
         old_raw_stable=None, new_raw_stable=None, covered_pointers=None) -> list[dict]:
    """两版的差异。两边相同（含都缺）的字段不出现。old/new_raw_stable 是去掉来源易变字段后的原件：
    去掉已覆盖路径后的剩余部分若有变化，记 unmapped_change（不论是否已有别的变化）。"""
    locators = locators or {}
    changes = []
    for key, kind in FIELDS.items():
        a_present, b_present = key in old_facts, key in new_facts
        a, b = old_facts.get(key), new_facts.get(key)
        if a_present == b_present and _canonical(a) == _canonical(b):
            continue
        reason = immaterial_reason(key, a, b, a_present, b_present)
        changes.append(
            {
                "field": key,
                "change_kind": kind,
                "json_pointer": (locators.get(key) or {}).get("json_pointer"),
                "old_value": _stored(old_facts, key),
                "new_value": _stored(new_facts, key),
                "detail": _detail(key, a, b, a_present, b_present),
                "material": reason is None,
                "material_reason": reason or "not_provably_immaterial",
            }
        )
    if (old_summary or "") != (new_summary or ""):
        summaries = {"brief_summary": old_summary}, {"brief_summary": new_summary}
        changes.append(
            {
                "field": "brief_summary",
                "change_kind": "description_change",
                "json_pointer": SUMMARY_POINTER,
                "old_value": _stored(summaries[0], "brief_summary") if old_summary is not None else MISSING,
                "new_value": _stored(summaries[1], "brief_summary") if new_summary is not None else MISSING,
                "detail": {},
                "material": True,
                "material_reason": "not_provably_immaterial",
            }
        )
    old_rest = remainder(old_raw_stable, covered_pointers)
    new_rest = remainder(new_raw_stable, covered_pointers)
    if old_rest is not None and new_rest is not None and old_rest != new_rest:
        changes.append(
            {
                "field": "_unmapped",
                "change_kind": "unmapped_change",
                "json_pointer": None,
                "old_value": {"present": True, "sha256": hashlib.sha256(old_rest.encode()).hexdigest()},
                "new_value": {"present": True, "sha256": hashlib.sha256(new_rest.encode()).hexdigest()},
                "detail": {"note": "原件在已投影字段和简介之外有变化，需读两版原件",
                           "changed_paths": _changed_paths(old_rest, new_rest)},
                "material": True,
                "material_reason": "not_provably_immaterial",
            }
        )
    return changes


def change_id(from_version_id: str, to_version_id: str, field: str) -> str:
    return hashlib.sha256(
        _canonical([PROCESSOR_VERSION, from_version_id, to_version_id, field]).encode()
    ).hexdigest()


def describe(change: dict) -> str:
    """一行中文说明，给雷达和报告用；只复述两版的值，不加推断，缺失写缺失。"""

    def show(stored):
        if not stored.get("present"):
            return "（原件无此字段）"
        if "value" in stored:
            text = _canonical(stored["value"])
            return text if len(text) <= 200 else text[:200] + "…"
        return f"（长字段，{stored.get('chars', '?')} 字符，见原件）"

    if change["change_kind"] == "unmapped_change":
        return "原件有变化，但不在已投影的登记字段和简介里（需读两版原件）"
    head = f"{change['field']}（{change.get('json_pointer') or '?'}）: {show(change['old_value'])} → {show(change['new_value'])}"
    d = change.get("detail") or {}
    if "days" in d:
        head += f"；相差 {d['days']} 天（{d['precision']} 精度）"
    if "delta" in d:
        head += f"；变化 {d['delta']:+d}"
    if d.get("added") or d.get("removed"):
        head += f"；新增 {d.get('added')}，移除 {d.get('removed')}"
    if d.get("same_names_content_changed"):
        head += "；名称未变但内容改了（如剂量、描述），需读原件"
    if "count_before" in d:
        before = "未知" if d["count_before"] is None else d["count_before"]
        after = "未知" if d["count_after"] is None else d["count_after"]
        head += f"；地点数 {before} → {after}"
    return head
