"""
名称规范化（Step 0）。
为后续各步生成候选名称列表，原始名优先，规范化名辅助。
"""

import re

from disambiguation.normalization_rules import (
    OCR_CORRECTIONS,
    PROCESSING_PREFIXES,
    PROCESSING_WHITELIST,
    DOSAGE_SUFFIXES,
    DOSAGE_WHITELIST,
    PROCESSING_ENTITY_TYPES,
    DOSAGE_ENTITY_TYPES,
    MIN_CORE_LENGTH,
)


def _fullwidth_to_halfwidth(text: str) -> str:
    """全角字符转半角。"""
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            result.append(' ')
        else:
            result.append(ch)
    return ''.join(result)


def _normalize_punctuation(text: str) -> str:
    """统一中英文括号和标点。"""
    text = text.replace('（', '(').replace('）', ')')
    text = text.replace('「', '[').replace('」', ']')
    text = text.replace('｛', '{').replace('｝', '}')
    text = text.replace('，', ',').replace('；', ';').replace('：', ':')
    return text


def _remove_company_suffix(name: str) -> str:
    """去除公司法律后缀：有限公司、Inc.、Ltd. 等。"""
    cn_suffixes = [
        '股份有限公司',      # 股份有限公司
        '有限责任公司',      # 有限责任公司
        '有限公司',                  # 有限公司
        '股份公司',                  # 股份公司
    ]
    result = name
    for suffix in cn_suffixes:
        if result.endswith(suffix):
            result = result[:-len(suffix)]
            break

    eng_suffixes = [
        r'\s+Inc\.?$', r'\s+Ltd\.?$', r'\s+Limited$',
        r'\s+Co\.\s*,\s*Ltd\.?$', r'\s+Corp\.?$',
        r'\s+Corporation$', r'\s+Co\.?$', r'\s+PLC$',
        r'\s+LLC$', r'\s+LLP$',
    ]
    for pattern in eng_suffixes:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)

    return result.strip()


def _normalize_whitespace(text: str) -> str:
    """合并多余空白字符和连字符。"""
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[-－‐‑‒–—―]{2,}', '-', text)
    return text.strip()


def normalize_name(entity_name: str, entity_type: str = "") -> str:
    """
    通用规范化：全角→半角、括号统一、英文小写、空格归一。
    规范化名只作辅助匹配，原始名仍是主匹配名。
    """
    name = entity_name.strip()
    name = _fullwidth_to_halfwidth(name)
    name = _normalize_punctuation(name)
    name = name.lower()
    name = _normalize_whitespace(name)
    name = name.strip('.,;:!?。，；：！？、\xb7* ')
    return name


def generate_candidate_names(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None = None,
) -> list[str]:
    """
    生成候选名称列表，按优先级排序：
      1. 原始实体名
      2. entity_data.name
      3. entity_data.name_en
      4. entity_data.aliases
      5. 规范化原始名
      6. 规范化去公司后缀名（仅 Company 类型）
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(n: str):
        if not n or not isinstance(n, str):
            return
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)

    _add(entity_name)

    if entity_data:
        _add(entity_data.get("name", ""))
        _add(entity_data.get("name_en", ""))
        for alias in (entity_data.get("aliases") or []):
            if alias is not None:
                _add(str(alias))

    norm = normalize_name(entity_name, entity_type)
    _add(norm)

    if entity_type == "Company":
        no_suffix = _remove_company_suffix(norm)
        _add(no_suffix)

    return names


def _apply_ocr_corrections(name: str) -> str:
    """OCR 纠错：已知错误模式词典替换。只做已知错误，不做字符级全局替换。"""
    for wrong, right in OCR_CORRECTIONS.items():
        name = name.replace(wrong, right)
    return name


def _strip_processing_prefix(name: str) -> str:
    """剥离中药炮制前缀。白名单（保护前缀）优先；核心名长度 ≥ MIN_CORE_LENGTH 才剥。"""
    for protected in PROCESSING_WHITELIST:
        if name.startswith(protected):
            return name
    for prefix in PROCESSING_PREFIXES:
        if name.startswith(prefix) and len(name) - len(prefix) >= MIN_CORE_LENGTH:
            return name[len(prefix):]
    return name


def _strip_dosage_suffix(name: str) -> str:
    """剥离剂型后缀。长后缀优先；白名单（保护后缀）优先；核心名长度 ≥ MIN_CORE_LENGTH 才剥。"""
    for protected in DOSAGE_WHITELIST:
        if name.endswith(protected):
            return name
    for suffix in DOSAGE_SUFFIXES:
        if name.endswith(suffix) and len(name) - len(suffix) >= MIN_CORE_LENGTH:
            return name[:-len(suffix)]
    return name


def normalize_for_alignment(entity_name: str, entity_type: str = "") -> str:
    """
    对齐导向规范化（4.1）：剥离修饰成分、恢复核心名，生成 normalized_name 对齐辅助键。

    - OCR 纠错（全类型）
    - 炮制前缀剥离（Drug / TCMFormula）
    - 剂型后缀剥离（Drug / Compound）

    返回核心名；无剥离时返回去除首尾空白后的原 name。
    注意：返回值是「对齐辅助键」，不覆盖原文 entity_name；按设计仅用于 Step4 向量召回。
    """
    name = entity_name.strip()
    name = _apply_ocr_corrections(name)
    if entity_type in PROCESSING_ENTITY_TYPES:
        name = _strip_processing_prefix(name)
    if entity_type in DOSAGE_ENTITY_TYPES:
        name = _strip_dosage_suffix(name)
    return name
