"""国家药品监督管理局（NMPA）详情页特解"""
import re

from lxml import html as lxml_html

from clean.excel_parser import _rows_to_markdown
from fetch.extractors import extract_approval_number, extract_labeled_field
from fetch.extractors.dates import normalize_date


def extract_nmpa_detail(page) -> dict:
    result = {
        "title": "",
        "publish_date": "",
        "body_text": "",
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
        result["body_text"] = _body_text(body_el)

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


def _body_text(body_el) -> str:
    """正文文字：和 get_all_text(strip=True) 一样逐段取字、一段一行；正文里的表格（旧版页面常见）改成 Markdown 表格，
    一行一条记录，不再一格一行。"""
    if not body_el.css("table"):
        return body_el.get_all_text(strip=True)
    root = lxml_html.fromstring(body_el.html_content)
    for table in root.xpath(".//table[not(ancestor::table)]"):
        rows = [[" ".join(cell.text_content().split()) for cell in tr.xpath("./td|./th")] for tr in table.xpath(".//tr")]
        rows = [row for row in rows if any(row)]
        holder = lxml_html.Element("div")
        holder.text = _rows_to_markdown(rows)
        holder.tail = table.tail
        table.getparent().replace(table, holder)
    parts = (part.strip() for part in root.xpath(".//text()[not(ancestor::script) and not(ancestor::style)]"))
    return "\n".join(part for part in parts if part)
