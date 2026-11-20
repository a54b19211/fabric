"""中国医药创新促进会（PhIRDA）详情页特解"""
import re

from fetch.extractors.dates import normalize_date
from fetch.extractors.text_tools import block_text


def extract_phirda_detail(page) -> dict:
    result = {
        "title": "",
        "publish_date": "",
        "body_text": "",
        "attachment_links": [],
    }

    # 标题：<title> 标签，去掉站点后缀
    title_els = page.css("title")
    if title_els:
        raw = title_els[0].get_all_text(strip=True)
        result["title"] = re.sub(r"\s*[-_|]\s*中国.*$", "", raw).strip()

    # 日期：dl.xq_dl 里的 i 标签
    for i_el in page.css("dl.xq_dl i"):
        raw = i_el.get_all_text(strip=True)
        normalized = normalize_date(raw)
        if normalized:
            result["publish_date"] = normalized
            break

    # 正文：div.xq_con
    body_els = page.css("div.xq_con")
    body_el = body_els[0] if body_els else None

    if body_el:
        # 按段落取字：get_all_text(strip=True) 按文本节点分行，加粗的词、链接都会把一句话断开（“云端按语”一行、“：脉流……”另一行）
        result["body_text"] = block_text(body_el.html_content)

    # 附件：正文内的文件链接
    search_el = body_el or page
    for a in search_el.css("a"):
        href = a.attrib.get("href", "")
        name = a.get_all_text(strip=True) or "未命名附件"
        if href and re.search(r'\.(pdf|doc|docx|xls|xlsx|zip|wps)$', href, re.I):
            if href.startswith("/"):
                href = "https://www.phirda.com" + href
            result["attachment_links"].append({"name": name, "url": href})

    return result
