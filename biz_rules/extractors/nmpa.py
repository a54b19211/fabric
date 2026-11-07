"""国家药品监督管理局（NMPA）详情页特解"""
import re

from biz_rules.cleaners.field.date_format import normalize_date
from biz_rules.extractors import extract_approval_number, extract_labeled_field


def _extract_summary(body_text: str, max_len: int = 200) -> str:
    """
    从正文中提取有效摘要，跳过页面结构噪音行（索引号、主题分类、标题重复等）。
    取第一段实质性内容。
    """
    noise_prefixes = ("索引号", "主题分类", "标题", "发布日期", "发布时间", "JGXX-")
    lines = body_text.splitlines()
    content_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in noise_prefixes):
            continue
        # 跳过纯日期行
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", stripped):
            continue
        content_lines.append(stripped)
        if sum(len(ln) for ln in content_lines) >= max_len:
            break
    summary = " ".join(content_lines)
    return summary[:max_len]


def extract_nmpa_detail(page) -> dict:
    result = {
        "title": "",
        "publish_date": "",
        "body_html": "",
        "body_text": "",
        "summary": "",
        "attachment_links": [],
        "extra": {},
    }

    # 标题：优先 meta ArticleTitle，其次 <title>
    meta_title = page.css("meta[name='ArticleTitle']")
    if meta_title:
        result["title"] = meta_title[0].attrib.get("content", "").strip()
    if not result["title"]:
        els = page.css("title")
        if els:
            result["title"] = els[0].get_all_text(strip=True)

    # 日期：meta PubDate，格式 "2026-02-25 09:22"
    date_els = page.css("meta[name='PubDate']")
    if date_els:
        raw = date_els[0].attrib.get("content", "").strip()
        result["publish_date"] = normalize_date(raw)

    # 正文：div.wenzhang
    body_els = page.css("div.wenzhang")
    body_el = body_els[0] if body_els else None
    if body_el:
        result["body_html"] = body_el.html_content
        result["body_text"] = body_el.get_all_text(strip=True)
        result["summary"] = _extract_summary(result["body_text"])

    # 来源特有字段：drug_name / approval_number / manufacturer（供 doc_extra 与血缘追踪）
    body_text = result["body_text"]
    result["extra"] = {
        "drug_name": extract_labeled_field(
            body_text, ("药品名称", "通用名称", "商品名称", "品种名称")
        ),
        "approval_number": extract_approval_number(body_text),
        "manufacturer": extract_labeled_field(
            body_text, ("生产企业", "生产单位", "企业名称", "申请人")
        ),
    }

    # 附件：正文区域内的文件链接
    if body_el:
        for a in body_el.css("a"):
            href = a.attrib.get("href", "")
            name = a.get_all_text(strip=True) or "未命名附件"
            if href and re.search(r'\.(pdf|doc|docx|xls|xlsx|zip|wps)$', href, re.I):
                if href.startswith("/"):
                    href = "https://www.nmpa.gov.cn" + href
                result["attachment_links"].append({"name": name, "url": href})

    return result
