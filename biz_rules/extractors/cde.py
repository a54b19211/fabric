"""国家药品审评中心（CDE）详情页特解"""
import re


def extract_cde_detail(page) -> dict:
    result = {
        "title": "",
        "publish_date": "",
        "body_html": "",
        "body_text": "",
        "attachment_links": [],
    }

    # 标题：meta ArticleTitle，其次 div.news_detail_title
    meta_title = page.css("meta[name='ArticleTitle']")
    if meta_title:
        result["title"] = meta_title[0].attrib.get("content", "").strip()
    if not result["title"]:
        els = page.css("div.news_detail_title")
        if els:
            result["title"] = els[0].get_all_text(strip=True)

    # 日期：meta PubDate，格式 20260403 → 2026-04-03
    date_els = page.css("meta[name='PubDate']")
    if date_els:
        raw = date_els[0].attrib.get("content", "").strip()
        m = re.match(r"(\d{4})(\d{2})(\d{2})$", raw)
        if m:
            result["publish_date"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        else:
            m2 = re.search(r"\d{4}-\d{1,2}-\d{1,2}", raw)
            if m2:
                result["publish_date"] = m2.group()

    # 正文：div.new_detail_content（新版）或 div.new_detail_content_old（旧版）
    body_els = page.css("div.new_detail_content") or page.css("div.new_detail_content_old")
    body_el = body_els[0] if body_els else None
    if body_el:
        result["body_html"] = body_el.html_content
        result["body_text"] = body_el.get_all_text(strip=True)

    # 附件：div.relatedNews 里的链接
    related = page.css("div.relatedNews")
    if related:
        for a in related[0].css("a"):
            href = a.attrib.get("href", "")
            name = a.get_all_text(strip=True) or "未命名附件"
            if href:
                if href.startswith("/"):
                    href = "https://www.cde.org.cn" + href
                result["attachment_links"].append({"name": name, "url": href})

    return result
