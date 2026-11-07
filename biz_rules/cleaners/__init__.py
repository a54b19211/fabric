"""
站点提取器用的字段工具（空白、全角、标题、日期）。正文清洗只在 processing.clean_text。
"""

import re


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

    alnum_only=False（默认）：全角 ASCII 区间 + 全角空格，与编号抽取用的是同一个实现
    （processing.identifiers.fold_fullwidth）；适合只关心数字、字母的场景。
    alnum_only=True：只转全角数字、全角字母和全角空格。U+FF01–FF5E 里也有中文标点（，；：！？（）），
    整段转换会把「主要目的：」改成「主要目的:」，处理中文字段值时用这一档。
    """
    if not text:
        return ""
    if alnum_only:
        return _FULLWIDTH_ALNUM.sub(lambda m: chr(ord(m.group()) - 0xFEE0), text.replace("\u3000", " "))
    from processing.identifiers import fold_fullwidth

    return fold_fullwidth(text)


def remove_extra_whitespace(text: str) -> str:
    # 薄封装：保留换行的空白规范化
    return normalize_whitespace(text, preserve_newlines=True)


def remove_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# 日期规范只在 field/date_format.py
from biz_rules.cleaners.field.date_format import normalize_date  # noqa: E402


def clean_title(title: str) -> str:
    title = remove_html_tags(title)
    title = normalize_whitespace(title)
    return title


__all__ = [
    "normalize_whitespace",
    "full_to_half",
    "remove_extra_whitespace",
    "remove_html_tags",
    "normalize_date",
    "clean_title",
]
