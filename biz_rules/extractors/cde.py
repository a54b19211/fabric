"""国家药品审评中心（CDE）详情页特解"""
from biz_rules.cleaners.field.date_format import normalize_date
from biz_rules.extractors import extract_approval_number, extract_labeled_field


def extract_cde_detail(page) -> dict:
    result = {
        "title": "",
        "publish_date": "",
        "body_html": "",
        "body_text": "",
        "attachment_links": [],
        "extra": {},
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
        result["publish_date"] = normalize_date(raw)

    # 正文：div.new_detail_content（新版）或 div.new_detail_content_old（旧版）
    body_els = page.css("div.new_detail_content") or page.css("div.new_detail_content_old")
    body_el = body_els[0] if body_els else None
    if body_el:
        result["body_html"] = body_el.html_content
        result["body_text"] = body_el.get_all_text(strip=True)

    # 来源特有字段：drug_name / approval_number / manufacturer（供 doc_extra 与血缘追踪）
    body_text = result["body_text"]
    result["extra"] = {
        "drug_name": extract_labeled_field(
            body_text, ("药品名称", "通用名称", "商品名称", "品种名称")
        ),
        "approval_number": extract_approval_number(body_text),
        "manufacturer": extract_labeled_field(
            body_text, ("生产企业", "生产单位", "企业名称", "申请人", "申办方")
        ),
    }

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
