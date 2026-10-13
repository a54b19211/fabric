"""
字段标准化 L3 —— 语义层归一（中文药学）。

L1 结构层（编码/空白/类型）与 L2 值域层（日期/枚举）由其它模块负责；
本模块聚焦 L3 语义层，做两件最常被"同名异物 / 异名同物"困扰的事：

1. 同义词归一：别名 / 商品名 → 标准通用名
   （如 阿司匹林 / Aspirin / 乙酰水杨酸 → 阿司匹林）
2. 单位换算：浓度 / 剂量单位**级联**归一
   （ng→μg→mg→g、mL→L 等，一路收敛到标准单位：质量→g、体积→L）

设计原则（与前期探索性原型对齐）：
- 未命中映射时**返回原值**，绝不覆盖或丢弃原始数据；
- 映射表是"种子"，后续按受控词表（ATC / EDQM / OLS4 等）持续扩充；
- 盐型 / 剂型剥离、剂量表达式深层解析等更高级语义归一，作为后续扩展点。
"""

from typing import Dict, Tuple

# ── 同义词归一表：原始写法(小写) → 标准通用名 ──
# 键统一小写；匹配时对输入做「去空格 + 小写」归一。
_SYNONYM_MAP: Dict[str, str] = {
    # 解热镇痛 / 抗炎
    "阿司匹林": "阿司匹林",
    "aspirin": "阿司匹林",
    "乙酰水杨酸": "阿司匹林",
    "acetylsalicylic acid": "阿司匹林",
    "扑热息痛": "对乙酰氨基酚",
    "paracetamol": "对乙酰氨基酚",
    "acetaminophen": "对乙酰氨基酚",
    "对乙酰氨基酚": "对乙酰氨基酚",
    # 抗生素
    "头孢曲松": "头孢曲松",
    "ceftriaxone": "头孢曲松",
    # 降脂
    "阿托伐他汀": "阿托伐他汀",
    "atorvastatin": "阿托伐他汀",
    # 抗血小板
    "氯吡格雷": "氯吡格雷",
    "clopidogrel": "氯吡格雷",
    # 可继续按受控词表扩充……
}

# ── 单位换算表：源单位(小写) → (目标标准单位, 换算系数) ──
# 质量统一收敛到 g，体积统一收敛到 L。
_UNIT_MAP: Dict[str, Tuple[str, float]] = {
    "ng": ("μg", 1e-3),
    "μg": ("mg", 1e-3),
    "ug": ("mg", 1e-3),
    "mg": ("g", 1e-3),
    "g": ("g", 1.0),
    "kg": ("g", 1e3),
    "μl": ("mL", 1e-3),
    "ul": ("mL", 1e-3),
    "ml": ("L", 1e-3),
    "l": ("L", 1.0),
}

# 需要做单位换算的 field_type 取值（含中英文）
_UNIT_FIELD_TYPES = {
    "dosage", "concentration", "amount", "unit",
    "剂量", "浓度", "含量",
}


def _norm_key(text: str) -> str:
    """去空格 + 小写，作为映射表检索键。"""
    return (text or "").strip().lower()


def normalize_synonym(raw_value: str) -> str:
    """
    同义词归一：别名 / 商品名 → 标准通用名。

    未命中映射表时返回原值（不丢数据）。
    """
    if not raw_value:
        return raw_value
    return _SYNONYM_MAP.get(_norm_key(raw_value), raw_value)


def normalize_unit_expression(raw_value: str) -> str:
    """
    单位换算：把「数值 + 单位」表达式级联归一为标准单位。

    形如 "500 mg" / "0.5g" / "100μg" → 返回 "0.5 g" / "0.5 g" / "0.0001 g"。
    换算会一路级联收敛到标准单位（质量→g、体积→L），例如
    μg→mg→g、mL→L，确保同一物理量落到同一字符串表示。
    无法解析（无数字 / 单位不在表内）时返回原值。
    """
    import re

    if not raw_value:
        return raw_value
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-zμ°]+)\s*", raw_value)
    if not m:
        return raw_value
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in _UNIT_MAP:
        return raw_value
    # 级联换算：反复套用 _UNIT_MAP，直到收敛到标准单位
    # （质量→g、体积→L；如 μg→mg→g，mL→L）
    while unit in _UNIT_MAP and _UNIT_MAP[unit][0] != unit:
        target, factor = _UNIT_MAP[unit]
        num = num * factor
        unit = target
    # :g 格式避免 0.5000000001 这类浮点噪音
    return f"{num:g} {unit}"


def normalize_l3(raw_value: str, field_type: str = "drug_name") -> str:
    """
    字段标准化 L3 统一入口。

    参数:
        raw_value:   原始字段值
        field_type:  字段类型，决定走哪条归一路径
                      - drug_name / 通用名 / 其它 → 同义词归一
                      - dosage / concentration / amount / unit / 剂量 / 浓度 → 单位换算

    返回:
        归一后的字符串；未命中映射时返回原值。
    """
    if not raw_value:
        return raw_value
    ft = _norm_key(field_type)
    if ft in _UNIT_FIELD_TYPES:
        return normalize_unit_expression(raw_value)
    # 默认：药物名 / 通用名同义词归一
    return normalize_synonym(raw_value)
