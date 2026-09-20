"""Fierce Pharma 详情页特解"""
import re

from biz_rules.cleaners.field.date_format import normalize_date


def extract_fiercepharma_detail(page) -> dict:
    result = {
        "title": "",
        "publish_date": "",
        "body_html": "",
        "body_text": "",
        "attachment_links": [],
    }

    # 标题：优先 h1.article-title，其次 og:title meta，最后 <title>
    for sel in ("h1.article-title", "h1.node-title", "h1"):
        els = page.css(sel)
        if els:
            result["title"] = els[0].get_all_text(strip=True)
            break
    if not result["title"]:
        og = page.css("meta[property='og:title']")
        if og:
            result["title"] = og[0].attrib.get("content", "").strip()
    if not result["title"]:
        t = page.css("title")
        if t:
            result["title"] = re.sub(r"\s*[|\-]\s*Fierce.*$", "", t[0].get_all_text(strip=True)).strip()

    # 日期：time[datetime]，或 meta article:published_time
    time_els = page.css("time[datetime]")
    if time_els:
        raw = time_els[0].attrib.get("datetime", "")
        result["publish_date"] = normalize_date(raw)
    if not result["publish_date"]:
        pub_meta = page.css("meta[property='article:published_time']")
        if pub_meta:
            raw = pub_meta[0].attrib.get("content", "")
            result["publish_date"] = normalize_date(raw)

    # 正文：article 标签，或 div.article-body / div.node-body
    body_el = None
    for sel in ("article", "div.article-body", "div.node-body", "div.field-body"):
        els = page.css(sel)
        if els:
            body_el = els[0]
            break

    if body_el:
        result["body_html"] = body_el.html_content
        result["body_text"] = body_el.get_all_text(strip=True)

    return result
