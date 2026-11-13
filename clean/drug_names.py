"""药名归一：把站点原始的“药品名称”字段切成能送权威库查询的候选名（徐子雯 T3 的前置一步，规则照她 11-13 版）。

为什么要切（她 2026-10-27 实测：18 条带药名的资料只有 4 条能在 PubChem 查到）：药名字段常常不是药名——
- openFDA 召回的 drug_name 是整段产品描述：'Ifosfamide Injection 3g/60 mL, Single dose vial, Rx only, …'；
- ClinicalTrials 是“代号 + 剂量”“A + B”：'JNJ-26866138 0.7 mg/m2'、'Nivolumab + Relatlimab'；
- 品牌名把通用名放括号里：'Enbrel (Etanercept)'。

规则：
1. 不改原值：候选名只用来查询；命中后对齐结果记下原值、实际查的候选名和用的规则（rule），能回溯，也能统计哪条规则贡献了命中。
2. 有序回退、命中就停：candidates() 按“越像规范名越靠前”排好，最多 MAX_CANDIDATES 个。
3. 宁缺毋滥：占位名（安慰剂、生理盐水、空白对照）不查，不进命中率的分母。
4. 中文名不动：中文药名里的“肠溶片”“注射液”是名字的一部分，纯中文串只返回它自己。
纯函数：不联网、不连库。
"""

from __future__ import annotations

import re

MAX_CANDIDATES = 4  # 一个药名最多试几个候选名（控制请求量）
MAX_NAME_LENGTH = 120  # 超长的不会是规范名
MIN_NAME_LENGTH = 3  # 'mL'、'b)' 这类残片不是药名
MIN_ALIAS_LENGTH = 3  # 括号里的 'R'、'TM' 是商标标记，不算别名
MAX_NAMES = 8  # 一份资料最多对齐几个药名（每个药名都要联网）
MAX_RAW_NAME = 500  # 原始药名最多留这么长（openFDA 的整段产品描述可能上千字；它是对齐表主键的一部分）

PLACEHOLDER_NAMES = {
    "placebo", "placebo comparator", "saline", "normal saline", "vehicle",
    "sham", "sham procedure", "no intervention", "standard of care",
    "安慰剂", "生理盐水", "空白对照", "对照", "无干预",
}

# 产品描述的边界（剂型、包装、监管标记、企业信息）：在它前面截断就是药名主干
_BOUNDARY = re.compile(
    r"(?<![A-Za-z])("
    r"tablets?|capsules?|injections?|injectables?|solutions?|suspensions?|"
    r"sprays?|creams?|ointments?|gels?|lotions?|patches?|suppositor\w*|"
    r"lozenges?|granules?|powders?|syrups?|drops|vials?|bags?|syringes?|"
    r"rub|foam|shampoos?|soaps?|wipes?|swabs?|"
    r"rx\s*only|ndc|upc|"
    r"mfd\s+by|manufactured\s+by|distributed\s+by|marketed\s+by|"
    r"packaged\s+as|packaged\s+by|compounded\s+by|service\s+code|imported\s+by"
    r")\b",
    re.IGNORECASE,
)
# 剂量规格（数字 + 单位，单位后可再跟 /mL、/4 mL 这类分母，一起去掉，否则残留 "mL " 前缀）
_DOSE = re.compile(
    r"(?<![A-Za-z0-9.\-])"
    r"\d[\d.,]*\s*"
    r"(?:mcg|μg|ug|mg|kg|g|ml|l|iu|units?|%|fl\s*oz)"
    r"(?:\s*/\s*\d*[\d.,]*\s*(?:mcg|μg|ug|mg|kg|g|ml|l|m2|m\^2|dose))?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 明显不是药名的片段（包装、企业、地址、监管标记）：逗号段里有它就不要
_JUNK = re.compile(
    r"(?<![A-Za-z])("
    r"rx\s*only|ndc|upc|service\s+code|packaged|distributed|manufactured|"
    r"mfd|inc\.?|llc|ltd|corp|gmbh|pharm\s*d|glass\s+jar|bottle|vial|"
    r"single\s+dose|multidose|sterile|preservative|subcutaneous|intravenous|"
    r"intramuscular|injection\s+use|for\s+use|count|fl\s*oz|dosage|"
    r"not\s+for|keep\s+out|store\s+at"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)
_SEGMENT_MARKER = re.compile(r"^[a-z]\)\s*", re.IGNORECASE)  # 'b) 2 FL OZ …' 这类段落编号
_COMBO = re.compile(r"\s*(?:\+|&|\band\b|、)\s*", re.IGNORECASE)  # 组合药名取第一个成分
_PAREN = re.compile(r"[（(]([^（()）]{1,80})[)）]")
_TRAILING = re.compile(
    r"[\s,;:/|.\-–—]*(?:\b(?:in|for|with|of|by|and|or|the|a|an)\b[\s,;:/|.\-–—]*)*$",
    re.IGNORECASE,
)
_LEADING = re.compile(r"^[\s,;:/|.\-–—]+")
_NAME_SEPARATOR = re.compile(r"[;；]")  # ClinicalTrials 的多个干预药物用 "; " 拼在一起


def _clean_ws(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    return _LEADING.sub("", text).strip()


def is_placeholder(name: str) -> bool:
    """安慰剂、盐水、空白对照这类不是药品实体的名字。"""
    return _clean_ws(name).lower().strip(" .;,") in PLACEHOLDER_NAMES


def _plausible(value: str) -> bool:
    return (len(value) >= MIN_NAME_LENGTH and bool(re.search(r"[A-Za-z一-鿿]", value))
            and not _SEGMENT_MARKER.match(value) and not _JUNK.search(value))


def _strip_descriptors(text: str) -> str:
    """反复截掉剂型边界和剂量规格，直到不再变化（最多 3 轮）；剂量在最前面时删掉它继续。"""
    text = _clean_ws(text)
    for _ in range(3):
        before, cut = text, len(text)
        boundary = _BOUNDARY.search(text)
        if boundary and boundary.start() > 0:
            cut = min(cut, boundary.start())
        dose = _DOSE.search(text)
        if dose:
            if dose.start() == 0:
                text = _clean_ws(text[dose.end():])
                continue
            cut = min(cut, dose.start())
        text = _TRAILING.sub("", _clean_ws(text[:cut]))
        if text == before:
            break
    return _clean_ws(text)


def _inner_alias(text: str) -> str:
    """最长的一组括号内文字（品牌名后的通用名），跳过包装、监管类括号。"""
    groups = [g for g in (_clean_ws(g) for g in _PAREN.findall(text))
              if len(g) >= MIN_ALIAS_LENGTH and not _JUNK.search(g)]
    return max(groups, key=len) if groups else ""


def _comma_segments(text: str) -> list[str]:
    """第 2 到 4 个逗号段里含空格的多词片段（单个词在真实数据里都是地名、企业名，PubChem 会模糊匹配出假命中）。"""
    parts = [p.strip() for p in re.split(r"[,，]", text)]
    return [p for p in parts[1:4] if p and " " in p]


def candidates(raw: str) -> list[tuple[str, str]]:
    """[(候选名, 规则)]，去重保序，最多 MAX_CANDIDATES 个；空的、占位名返回 []（不该查）。
    规则：as_is 原样、cut_descriptor 截掉剂型和剂量、inner_alias 括号里的通用名、comma_segment 逗号后的片段、
    combo_split 组合药的第一个成分、strip_parenthetical 去掉括号后的商标主干、cjk_as_is 中文原样。"""
    raw = _clean_ws(raw)
    if not raw or is_placeholder(raw):
        return []
    if not re.search(r"[A-Za-z]", raw):
        return [(raw[:MAX_NAME_LENGTH], "cjk_as_is")]
    out, seen = [], set()

    def push(value, rule):
        value = _clean_ws(value)[:MAX_NAME_LENGTH]
        if value and value.lower() not in seen and _plausible(value) and not is_placeholder(value):
            seen.add(value.lower())
            out.append((value, rule))

    main = _strip_descriptors(raw)
    push(main, "as_is" if main == raw else "cut_descriptor")
    push(_strip_descriptors(_inner_alias(raw)), "inner_alias")
    for segment in _comma_segments(raw):
        value = _strip_descriptors(segment)
        if " " in value:  # 截完只剩一个词的（'Texas'）是地名
            push(value, "comma_segment")
    push(_strip_descriptors(_COMBO.split(main)[0]), "combo_split")
    if _PAREN.search(main):
        push(_PAREN.sub(" ", main), "strip_parenthetical")
    return out[:MAX_CANDIDATES]


def names_of(doc: dict) -> list[str]:
    """一份资料里待对齐的药名：只认 structured_data / extra 的 drug_name（字符串或列表），按 ; ； 拆开，去重保序。
    没有药名字段就是空列表（不拿标题顶替：整条标题送 PubChem 必然查不到）。"""
    raw = []
    for value in ((doc.get("structured_data") or {}).get("drug_name"), (doc.get("extra") or {}).get("drug_name")):
        if isinstance(value, str):
            raw.append(value)
        elif isinstance(value, (list, tuple)):
            raw.extend(str(v) for v in value if v not in (None, ""))
    names = []
    for value in raw:
        for part in _NAME_SEPARATOR.split(value):
            name = part.strip()[:MAX_RAW_NAME]
            if name and name not in names:
                names.append(name)
    return names
