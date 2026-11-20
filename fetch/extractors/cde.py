"""国家药品审评中心（CDE）详情页特解"""
import re

from fetch.extractors.dates import normalize_date
from fetch.extractors.text_tools import block_text

# 文件下载链接：/main/att/download/<id>、/zdyz/downloadAtt?idCODE=…，或者网址直接以文件后缀结尾
_FILE_LINK = re.compile(r"/att/download/|downloadAtt|\.(?:pdf|docx?|xlsx?|zip|rar|wps)(?:\?|$)", re.I)


def extract_cde_detail(page) -> dict:
    result = {
        "title": "",
        "publish_date": "",
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

    # 正文：div.new_detail_content（新版）或 div.new_detail_content_old（旧版）。
    # 指导原则专栏（/zdyz/）的详情页没有正文区，只有一张信息表（名称、适用范围、版本状态……），取它的各行（不含“意见留言”）。
    body_els = page.css("div.new_detail_content") or page.css("div.new_detail_content_old")
    body_el = body_els[0] if body_els else None
    if body_el:
        result["body_text"] = block_text(body_el.html_content)  # 按段落分行，行内的加粗、链接不断句
    else:
        info_table = page.css("table.layui-table")
        if info_table:
            rows = info_table[0].css("tr:not(.adviceInput)")
            result["body_text"] = "\n".join(row.get_all_text(separator=" ", strip=True) for row in rows)

    # 以前这里在正文里找“申请人”“企业名称”后面的几个字当 drug_name / manufacturer，抽出来的多是半句话
    # （“(以下简称申请人)可以在提交……”），不再抽：extra 为空

    # 附件：div.relatedNews 里的文件下载链接；指导原则专栏是信息表里指向 /zdyz/downloadAtt 的链接。
    # relatedNews 里还有“相关新闻”的页面链接（/main/news/viewInfoCommon/…、/main/newspic/view/…）：那是别的页面，不是附件
    related = page.css("div.relatedNews")
    for a in related[0].css("a") if related else page.css("a[href*='downloadAtt']"):
        href = a.attrib.get("href", "")
        name = a.get_all_text(strip=True) or "未命名附件"
        if href and _FILE_LINK.search(href):
            if href.startswith("/"):
                href = "https://www.cde.org.cn" + href
            result["attachment_links"].append({"name": name, "url": href})

    return result
