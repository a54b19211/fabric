"""临床登记的确定性差分：同一登记号前后两个固定版本，比较已投影的登记字段、简介和原件其余部分。

产出的是可核对的观测——“登记状态从 RECRUITING 改为 TERMINATED”，带两版的完整版本号和 JSON 路径。
它不判断临床成败或疗效：登记状态改变不等于药物失败，日期推迟不等于方案出问题。

v2（Codex r58 反例后）：分诊路由从“列出值得判断的变化”改为“只有能证明不影响判断的变化才不送模型”。
- 白名单（material=false）只有三种：字段值只差大小写/空白/列表顺序；地点列表只差联系人信息；
  正式题目只差大小写或空白。
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

PROCESSOR_VERSION = "datafabric.registry_diff.v2"

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


def _loose(value):
    """只用于判断“是否只差大小写/空白/列表顺序”：字符串小写并压空白，列表排序。"""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip().casefold()
    if isinstance(value, list):
        return sorted((_loose(v) for v in value), key=_canonical)
    if isinstance(value, dict):
        return {k: _loose(v) for k, v in value.items()}
    return value


def _without_contacts(sites):
    if not isinstance(sites, list):
        return sites
    return [{k: v for k, v in s.items() if k not in SITE_CONTACT_KEYS} if isinstance(s, dict) else s for s in sites]


def immaterial_reason(key, old, new, old_present, new_present) -> str | None:
    """能证明不影响判断时给出理由；否则 None（要送模型）。字段出现或消失一律不算。"""
    if not (old_present and new_present):
        return None
    if _canonical(_loose(old)) == _canonical(_loose(new)):
        return "case_spacing_or_order_only"
    if key == "locations" and _canonical(_without_contacts(old)) == _canonical(_without_contacts(new)):
        return "site_contacts_only"
    return None


def _detail(key, old, new, old_present, new_present) -> dict:
    """描述用的细节（天数、增减、名称增删、地点数），只复述，不参与“是否送模型”。缺失就是未知。"""
    kind = FIELDS[key]
    detail = {}
    if kind == "enrollment_change" and isinstance(old, int) and isinstance(new, int):
        detail["delta"] = new - old
    elif kind == "date_change":
        a, pa = _parse_date(old)
        b, pb = _parse_date(new)
        if a and b:
            days = (b - a).days
            detail.update(
                days=days,
                precision="day" if pa == pb == "day" else "month",
                direction="later" if days > 0 else "earlier" if days < 0 else "same_at_precision",
            )
    elif kind in {"condition_change", "intervention_change", "arm_change", "primary_outcome_change",
                  "secondary_outcome_change"}:
        before, after = _names(old), _names(new)
        detail["added"] = [x for x in after if x not in before][:20]
        detail["removed"] = [x for x in before if x not in after][:20]
        if not detail["added"] and not detail["removed"]:
            detail["same_names_content_changed"] = True  # 同名项内容变了（如剂量、描述），要看原件
    elif kind == "location_change":
        detail["count_before"] = len(old) if old_present and isinstance(old, list) else None
        detail["count_after"] = len(new) if new_present and isinstance(new, list) else None
    return detail


def diff(old_facts: dict, new_facts: dict, locators: dict | None = None, *, old_summary=None, new_summary=None,
         old_raw_stable=None, new_raw_stable=None) -> list[dict]:
    """两版的差异。两边相同（含都缺）的字段不出现。old/new_raw_stable 是去掉来源易变字段后的原件，
    用来发现投影字段之外的变化。"""
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
    if not changes and old_raw_stable is not None and new_raw_stable is not None and old_raw_stable != new_raw_stable:
        changes.append(
            {
                "field": "_unmapped",
                "change_kind": "unmapped_change",
                "json_pointer": None,
                "old_value": {"present": True, "sha256": hashlib.sha256(old_raw_stable.encode()).hexdigest()},
                "new_value": {"present": True, "sha256": hashlib.sha256(new_raw_stable.encode()).hexdigest()},
                "detail": {"note": "原件在已投影字段和简介之外有变化，需读两版原件"},
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
