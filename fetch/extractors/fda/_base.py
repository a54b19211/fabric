"""FDA 详情页特解公共基础函数"""
import re
from typing import List, Dict

from fetch.extractors.dates import normalize_date


def _extract_date(page) -> str:
    for sel in ["time[datetime]", ".article-date", ".news-date", ".date"]:
        els = page.css(sel)
        if els:
            raw = els[0].attrib.get("datetime", "") or els[0].get_all_text(strip=True)
            normalized = normalize_date(raw)
            if normalized:
                return normalized
    return ""


def _extract_attachments(page) -> List[Dict]:
    attachments = []
    for a in page.css("a[href]"):
        href = a.attrib.get("href", "")
        if re.search(r"\.(pdf|doc|docx|xls|xlsx|zip)$", href, re.I):
            if href.startswith("/"):
                href = "https://www.fda.gov" + href
            attachments.append({"name": a.get_all_text(strip=True) or "附件", "url": href})
    return attachments


def extract_base(page, body_selectors: List[str]) -> Dict:
    result = {
        "title": "",
        "publish_date": "",
        "body_text": "",
        "attachment_links": [],
    }

    for sel in ["h1.content-title", "h1"]:
        els = page.css(sel)
        if els:
            result["title"] = els[0].get_all_text(strip=True)
            break

    result["publish_date"] = _extract_date(page)

    for sel in body_selectors:
        els = page.css(sel)
        if els:
            result["body_text"] = els[0].get_all_text(strip=True)
            break

    result["attachment_links"] = _extract_attachments(page)
    return result
