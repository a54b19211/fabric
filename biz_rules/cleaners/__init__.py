"""
文本清洗工具函数 + 站点清洗器导出。

原 biz_rules/cleaners.py 的内容迁移至此，保持向后兼容。
"""

import re

# ── 通用文本清洗函数（原 cleaners.py）──────────────────────────────────────

def remove_extra_whitespace(text: str) -> str:
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def remove_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def normalize_date(date_str: str) -> str:
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", date_str)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"
    match = re.search(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", date_str)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return date_str.strip()


def clean_title(title: str) -> str:
    title = remove_html_tags(title)
    title = remove_extra_whitespace(title)
    return title


def truncate_text(text: str, max_length: int = 5000) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length] + "\n\n...[文本已截断]..."


# ── 站点清洗器 ──────────────────────────────────────────────────────────────

from biz_rules.cleaners.fiercepharma import clean_fiercepharma
from biz_rules.cleaners.clinical_trial import clean_clinical_trial

__all__ = [
    "remove_extra_whitespace",
    "remove_html_tags",
    "normalize_date",
    "clean_title",
    "truncate_text",
    "clean_fiercepharma",
    "clean_clinical_trial",
]
