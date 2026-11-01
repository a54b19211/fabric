"""
文本清洗工具函数 + 站点清洗器导出。

原 biz_rules/cleaners.py 的内容迁移至此，保持向后兼容。
"""

import re

# ── 通用文本清洗函数（原 cleaners.py）──────────────────────────────────────


def normalize_whitespace(text: str, preserve_newlines: bool = False) -> str:
    """
    规范化空白字符：处理 nbsp、全角空格、Tab、连续空白、多余换行。

    preserve_newlines=True 时保留换行（markdown 正文用），
    否则将所有连续空白折叠为单个空格（结构化字段值用）。
    """
    if not text:
        return ""
    # 统一不可见空白：nbsp、全角空格、Tab → 普通空格
    text = text.replace("\u00a0", " ").replace("\u3000", " ").replace("\t", " ")
    if preserve_newlines:
        # 行内连续空白 → 单个空格；连续 3+ 换行 → 双换行
        text = re.sub(r"[^\S\n]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        # 所有连续空白 → 单个空格
        text = re.sub(r"\s+", " ", text)
    return text.strip()


_FULLWIDTH_ALNUM = re.compile(r"[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")


def full_to_half(text: str, alnum_only: bool = False) -> str:
    """全角字符转半角。

    alnum_only=False（默认，向后兼容）：转换整个 U+FF01–FF5E 区段 + 全角空格。
        适用于标识符抽取等「只关心数字/字母、标点怎么变都行」的场景。

    alnum_only=True：只转换全角数字、全角字母、全角空格。
        清洗管线必须用这一档：U+FF01–FF5E 里同时住着中文标点
        （，；：！？（）），整段转半角会把「主要目的：」改成「主要目的:」，
        对中文语料是降质而非清洗。实测 150 条信封的字段值中
        全角字母数字 0 个、全角中文标点 408 个，整段转换 100% 有害。
    """
    if not text:
        return ""
    text = text.replace("\u3000", " ")
    if alnum_only:
        return _FULLWIDTH_ALNUM.sub(lambda m: chr(ord(m.group()) - 0xFEE0), text)
    return "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
        for ch in text
    )


def remove_extra_whitespace(text: str) -> str:
    # 薄封装：保留换行的空白规范化
    return normalize_whitespace(text, preserve_newlines=True)


def remove_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# normalize_date 统一收敛到 field/date_format.py，避免重复定义。
# 此处保留导出以兼容旧的 from biz_rules.cleaners import normalize_date 用法。
from biz_rules.cleaners.field.date_format import normalize_date  # noqa: E402
from biz_rules.cleaners.normalizer_l3 import (  # noqa: E402
    normalize_l3,
    normalize_synonym,
    normalize_unit_expression,
)


def normalize_publish_date(parsed_result: dict) -> dict:
    """标准化 publish_date（顶层 + structured_data 同步），返回原 parsed_result。

    无法解析时保留原值，不做覆盖（避免数据丢失）。
    """
    raw = parsed_result.get("publish_date", "")
    normalized = normalize_date(raw)
    if normalized:
        parsed_result["publish_date"] = normalized
        structured = parsed_result.get("structured_data")
        if isinstance(structured, dict):
            structured["publish_date"] = normalized
    return parsed_result


def clean_title(title: str) -> str:
    title = remove_html_tags(title)
    title = normalize_whitespace(title)
    return title


def truncate_text(text: str, max_length: int = 5000) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length] + "\n\n...[文本已截断]..."


# ── 站点清洗器 ──────────────────────────────────────────────────────────────

from biz_rules.cleaners.fiercepharma import clean_fiercepharma  # noqa: E402
from biz_rules.cleaners.clinical_trial import clean_clinical_trial  # noqa: E402

__all__ = [
    "normalize_whitespace",
    "full_to_half",
    "remove_extra_whitespace",
    "remove_html_tags",
    "normalize_date",
    "normalize_publish_date",
    "clean_title",
    "truncate_text",
    "clean_fiercepharma",
    "clean_clinical_trial",
    "normalize_l3",
    "normalize_synonym",
    "normalize_unit_expression",
]
