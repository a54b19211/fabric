"""
药名归一：把站点原始「药品名称」字段转成可送权威库查询的候选名（T3 前置步骤）。

为什么需要（2026-10-27 实测）：
    18 条带药名的信封里，只有 4 条能在 PubChem 命中（22.2%）。失败原因不是
    「取不到药名」，而是「药名不是药名」——
      - `fda_api_enforcement` 的 `drug_name` 实际映射到 `product_description`，
        值是整段产品描述：
            'Ifosfamide Injection 3g/60 mL, Single dose vial, Rx only, ... NDC 0069-4496-22'
        整段拿去查 PubChem 必然 404，而单独查 `Ifosfamide` 是能命中的。
      - `clinicaltrials` 是「试验代号 + 剂量」和「A + B」组合：
            'JNJ-26866138 0.7 mg/m2'、'Nivolumab + Relatlimab'
      - 品牌名把通用名放在括号里：
            'Enbrel (Etanercept)'、'VIRACEPT (R) (nelfinavir mesylate)'

设计原则：
    1. **不改原值**：`extra.drug_name` 保持采集原样，候选名只用于「查询」，
       命中后由对齐结果记录 `raw_value` / `candidate` / `rule` 三元组，可回溯。
    2. **规则可解释**：每个候选名都标注来源规则，便于论文统计「哪条归一规则
       贡献了多少命中」，而不是一个黑盒短语抽取。
    3. **有序回退、命中即止**：候选按「越可能是规范名」排序，命中后不再发请求。
    4. **宁缺毋滥**：占位名（Placebo / 安慰剂 / 生理盐水）不送权威库，
       标 `skipped` 而非 `not_found`——两者在论文里是不同性质的负例。
    5. **中文名不动刀**：中文药品名里的「肠溶片 / 注射液」是名称的一部分，
       不能按英文剂型边界截断。纯 CJK 串只返回自身。

典型输出（真实数据）：
    'Ifosfamide Injection 3g/60 mL, ...'      → 'Ifosfamide'
    'Enbrel (Etanercept)'                      → 'Etanercept'
    '2 mcg/mL Fentanyl Citrate and 0.16% ...'  → 'Fentanyl Citrate'
    'No Drip Nasal Spray, Oxymetazoline HCl ...' → 'Oxymetazoline HCl'
"""

import re
from dataclasses import dataclass
from typing import List

# 单次对齐最多尝试的候选名个数（控制外部请求量）
MAX_CANDIDATES_PER_NAME = 4
# 单条候选名最大长度（超长必然不是规范名，截断也白查，直接丢弃）
MAX_NAME_LENGTH = 120
# 候选名最小长度（'mL'、'b)' 这类残片不是药名）
MIN_NAME_LENGTH = 3
# 括号内别名的最小长度（'R'、'TM' 这类商标标记不算别名）
MIN_ALIAS_LENGTH = 3

# ── 占位名：不是药品实体，送权威库只会污染命中率统计 ──────────────────
PLACEHOLDER_NAMES = {
    "placebo", "placebo comparator", "saline", "normal saline", "vehicle",
    "sham", "sham procedure", "no intervention", "standard of care",
    "安慰剂", "生理盐水", "空白对照", "对照", "无干预",
}

# ── 产品描述边界：在此词之前截断即得药名主干 ─────────────────────────
# 剂型 / 包装 / 监管标记 / 企业信息 / 申请号编码
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

# ── 剂量/规格：数字 + 单位，出现在此之前的是药名，之后的是规格描述 ──────
# 单位后可再跟 /mL、/4 mL 这类浓度分母，必须一起吃掉，否则会残留 "mL " 前缀
_DOSE = re.compile(
    r"(?<![A-Za-z0-9.\-])"
    r"\d[\d.,]*\s*"
    r"(?:mcg|μg|ug|mg|kg|g|ml|l|iu|units?|%|fl\s*oz)"
    r"(?:\s*/\s*\d*[\d.,]*\s*(?:mcg|μg|ug|mg|kg|g|ml|l|m2|m\^2|dose))?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# ── 明显不是药名的片段：包装/企业/地址/监管标记 ───────────────────────
# 出现在逗号分段候选里，必须剔除，否则会把 "Rx only"、"Houston" 当药名查
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
# 段落编号残留（'b) 2 FL OZ ...'）
_SEGMENT_MARKER = re.compile(r"^[a-z]\)\s*", re.IGNORECASE)

# ── 组合药名：「A + B」「A and B」「A/B」取首个成分 ────────────────────
_COMBO = re.compile(r"\s*(?:\+|&|\band\b|、)\s*", re.IGNORECASE)

# ── 括号组：取最长的一组作为别名候选 ─────────────────────────────────
_PAREN = re.compile(r"[（(]([^（()）]{1,80})[)）]")

# ── 尾部连接词/标点：截断后常留下这些残渣 ───────────────────────────
_TRAILING = re.compile(
    r"[\s,;:/|.\-–—]*(?:\b(?:in|for|with|of|by|and|or|the|a|an)\b[\s,;:/|.\-–—]*)*$",
    re.IGNORECASE,
)
_LEADING = re.compile(r"^[\s,;:/|.\-–—]+")


@dataclass(frozen=True)
class Candidate:
    """一个候选药名及其来源规则。"""

    value: str
    rule: str

    def as_dict(self) -> dict:
        return {"value": self.value, "rule": self.rule}


def _clean_ws(text: str) -> str:
    """折叠空白、去首尾标点，不动词内字符。"""
    text = re.sub(r"\s+", " ", text or "")
    return _LEADING.sub("", text).strip()


def _is_cjk_only(text: str) -> bool:
    """是否不含拉丁字母（中文名不做英文剂型截断）。"""
    return not re.search(r"[A-Za-z]", text)


def _is_plausible_name(value: str) -> bool:
    """候选名是否像一个药名（剔除包装、企业、监管标记类残片）。"""
    if len(value) < MIN_NAME_LENGTH:
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", value):
        return False
    if _SEGMENT_MARKER.match(value):
        return False
    if _JUNK.search(value):
        return False
    return True


def is_placeholder(name: str) -> bool:
    """是否为占位名（安慰剂/盐水/空白对照），应跳过权威库查询。"""
    return _clean_ws(name).lower().strip(" .;,") in PLACEHOLDER_NAMES


def _strip_descriptors(text: str) -> str:
    """反复截断产品描述边界与剂量规格，直到稳定（最多 3 轮）。

    每轮取「边界词位置」与「剂量位置」中更靠前的一个作为截断点；
    若剂量恰好出现在开头（'2 mcg/mL Fentanyl Citrate'），则删掉该剂量继续。
    """
    text = _clean_ws(text)
    for _ in range(3):
        before = text
        cut = len(text)

        m = _BOUNDARY.search(text)
        if m and m.start() > 0:
            cut = min(cut, m.start())

        d = _DOSE.search(text)
        if d:
            if d.start() == 0:
                text = _clean_ws(text[d.end():])
                continue
            cut = min(cut, d.start())

        if cut < len(text):
            text = text[:cut]
        text = _TRAILING.sub("", _clean_ws(text))
        if text == before:
            break
    return _clean_ws(text)


def _inner_alias(text: str) -> str:
    """取最长的括号内文本（品牌名后的通用名），跳过包装/监管类括号内容。"""
    groups = [_clean_ws(g) for g in _PAREN.findall(text)]
    groups = [
        g for g in groups
        if len(g) >= MIN_ALIAS_LENGTH and not _JUNK.search(g)
    ]
    if not groups:
        return ""
    return max(groups, key=len)


def _comma_segments(text: str) -> List[str]:
    """按逗号切分，返回第 2 段起的内容（第 1 段已由主干候选覆盖）。

    只接受「含空格的多元词片段」：单 token 片段在真实数据里全是地名/企业名
    （'Chesapeake'、'Houston'、'Texas'），而 PubChem 的名称检索对这类词会做
    模糊匹配并返回无关化合物，制造假命中（实测 'Texas' 确实返回了 CID）。
    """
    parts = [p.strip() for p in re.split(r"[,，]", text)]
    return [p for p in parts[1:4] if p and " " in p]


def generate_candidates(raw: str) -> List[Candidate]:
    """生成有序候选名列表（去重保序，最多 MAX_CANDIDATES_PER_NAME 个）。

    返回空列表表示「不该送查」（空值或占位名）。
    """
    raw = _clean_ws(raw)
    if not raw or is_placeholder(raw):
        return []

    # 纯中文名：不做英文剂型截断，原样单候选
    if _is_cjk_only(raw):
        return [Candidate(raw[:MAX_NAME_LENGTH], "cjk_as_is")]

    seen = set()
    out: List[Candidate] = []

    def push(value: str, rule: str) -> None:
        value = _clean_ws(value)[:MAX_NAME_LENGTH]
        if not value or value.lower() in seen:
            return
        if not _is_plausible_name(value) or is_placeholder(value):
            return
        seen.add(value.lower())
        out.append(Candidate(value, rule))

    # 1) 主干：截断产品描述后的药名（本身就是规范名时与原文相同）
    main = _strip_descriptors(raw)
    push(main, "as_is" if main == raw else "cut_descriptor")

    # 2) 括号别名：品牌名后的通用名（'Enbrel (Etanercept)' → 'Etanercept'）
    push(_strip_descriptors(_inner_alias(raw)), "inner_alias")

    # 3) 逗号后的实义片段（'No Drip Nasal Spray, Oxymetazoline HCl ...'）
    #    截断后仍须是多元词：'Texas --- NDC ...' 截断后只剩 'Texas'，属地名，剔除
    for seg in _comma_segments(raw):
        value = _strip_descriptors(seg)
        if " " in value:
            push(value, "comma_segment")

    # 4) 组合药名取首个成分（'Nivolumab + Relatlimab' → 'Nivolumab'）
    push(_strip_descriptors(_COMBO.split(main)[0]), "combo_split")

    # 5) 去掉括号后剩余的商标主干（'VIRACEPT (R) ...' → 'VIRACEPT'）
    if _PAREN.search(main):
        push(_PAREN.sub(" ", main), "strip_parenthetical")

    return out[:MAX_CANDIDATES_PER_NAME]


def pick_primary(raw: str) -> str:
    """返回首选候选名（无候选时返回清洗后的原文，保证不丢值）。"""
    cands = generate_candidates(raw)
    return cands[0].value if cands else _clean_ws(raw)
