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


def full_to_half(text: str) -> str:
    """全角字符转半角（仅 ASCII 全角区间 + 全角空格）。"""
    if not text:
        return ""
    result = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:  # 全角空格 → 半角空格
            result.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII → 半角
            result.append(chr(code - 0xFEE0))
        else:
            result.append(ch)
    return "".join(result)


def remove_extra_whitespace(text: str) -> str:
    # 薄封装：保留换行的空白规范化
    return normalize_whitespace(text, preserve_newlines=True)


def remove_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# normalize_date 统一收敛到 field/date_format.py，避免重复定义。
# 此处保留导出以兼容旧的 from biz_rules.cleaners import normalize_date 用法。
from biz_rules.cleaners.field.date_format import normalize_date  # noqa: E402


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
    "clean_title",
    "truncate_text",
    "clean_fiercepharma",
    "clean_clinical_trial",
]
