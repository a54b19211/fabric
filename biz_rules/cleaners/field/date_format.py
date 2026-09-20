"""
日期标准化工具。

将各站点 extractor 提取的原始日期字符串统一为 YYYY-MM-DD 格式。
覆盖 11+ 种常见日期格式，无法解析时返回空字符串。
"""
import re
from datetime import datetime

# 英文月份全称/缩写 → 数字
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _safe_date(year: int, month: int, day: int) -> str:
    """构造合法日期，非法返回空字符串。"""
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def normalize_date(value) -> str:
    """
    将各种日期字符串标准化为 YYYY-MM-DD。

    支持格式：
      1. 2026-02-05 / 2026/2/5 / 2026.2.5（补零/不补零）
      2. 20260205（紧凑无分隔）
      3. 2026年2月5日（中文全称）
      4. February 5, 2026 / Feb 5, 2026（英文全称/缩写）
      5. 2026 Feb / 2026-02（PubMed 年月 / 年月，缺日补 01）
      6. 2026-02-05T10:20:30 / 2026-02-05 10:20:30（带时间戳）
      7. 2026年2月（中文年月，缺日补 01）
      8. 2026（纯年份，补 01-01）
      9. 05-Feb-2026（英文缩写倒序）
      10. 2026 年 2 月 5 日（带空格中文）
      11. 02/05/2026（美式月/日/年）

    无法解析返回空字符串。
    """
    if not value:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""

    # 1. YYYY-MM-DD / YYYY/M/D / YYYY.M.D（含补零/不补零）
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 2. 紧凑 YYYYMMDD
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 3. 中文全称：2026年2月5日（允许带空格）
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", raw)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 4. 英文全称/缩写 + 日 + 年：February 5, 2026 / Feb 5, 2026
    m = re.search(
        r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})",
        raw,
        re.IGNORECASE,
    )
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(2)))

    # 9. 英文缩写倒序：05-Feb-2026
    m = re.search(
        r"(\d{1,2})[-/. ]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-/. ]+(\d{4})",
        raw,
        re.IGNORECASE,
    )
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(1)))

    # 5. PubMed 年月：2026 Feb
    m = re.search(
        r"(\d{4})[-/. ]+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*",
        raw,
        re.IGNORECASE,
    )
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return _safe_date(int(m.group(1)), month, 1)

    # 6. 年月：2026-02 / 2026/2（缺日补 01）
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", raw)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), 1)

    # 7. 中文年月：2026年2月
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", raw)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), 1)

    # 8. 纯年份：2026
    m = re.fullmatch(r"(\d{4})", raw)
    if m:
        return _safe_date(int(m.group(1)), 1, 1)

    # 11. 美式月/日/年：02/05/2026
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", raw)
    if m:
        return _safe_date(int(m.group(3)), int(m.group(1)), int(m.group(2)))

    return ""
