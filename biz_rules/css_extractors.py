"""
==========================================================
  CSS/XPath 硬解规则 —— Fast Track VIP 通道
  ★ 药学研究生专属文件 ★ —— 针对固定网站的精确解析规则
==========================================================

【使用说明】
- 这个文件里的函数专门用来"硬解"特定网站的页面。
- "硬解"意思是：已知网页结构，直接用 CSS 选择器取值，100% 不调用大模型。
- 要添加新网站的硬解？模仿 extract_nmpa_detail 写一个新函数就行。
- 每个函数接收 HTML 文本，返回一个字典（包含标题、日期、正文等）。

【什么是 CSS 选择器？】
- 就像给 HTML 元素起的"地址"，比如：
  - "div.content" = class 叫 content 的 div 标签
  - "h1.article-title" = class 叫 article-title 的 h1 标签
  - "#main-content p" = id 叫 main-content 里面的所有 p 标签
"""

import re


# ======================== 药监局详情页硬解 ========================

# 把 CSS 选择器单独写成常量，方便随时修改
NMPA_SELECTORS = {
    "title": "title, h1, div.wenzhang h2, div.wenzhang h3",
    "date": "meta[name='PubDate']",          # <meta name="PubDate" content="2026-02-25 09:22">
    "body": "div.wenzhang",
    "attachments": "div.wenzhang a",
}


def extract_nmpa_detail(page) -> dict:
    """
    解析药监局（NMPA）公告详情页。

    参数:
        page: scrapling 返回的页面对象（支持 css / find 等方法）

    返回:
        dict，包含以下字段:
        {
            "title": "文档标题",
            "publish_date": "2024-03-15",
            "body_html": "正文 HTML",
            "body_text": "纯文本正文",
            "attachment_links": [{"name": "附件1.pdf", "url": "http://..."}],
        }
    """
    result = {
        "title": "",
        "publish_date": "",
        "body_html": "",
        "body_text": "",
        "attachment_links": [],
    }

    # --- 提取标题：优先 meta ArticleTitle，其次 <title> ---
    meta_title = page.css("meta[name='ArticleTitle']")
    if meta_title:
        result["title"] = meta_title[0].attrib.get("content", "").strip()
    if not result["title"]:
        title_els = page.css("title")
        if title_els:
            result["title"] = title_els[0].get_all_text(strip=True)

    # --- 提取日期：从 meta PubDate 取 content 属性 ---
    date_els = page.css("meta[name='PubDate']")
    if date_els:
        raw_date = date_els[0].attrib.get("content", "").strip()
        date_match = re.search(r"\d{4}-\d{1,2}-\d{1,2}", raw_date)
        if date_match:
            result["publish_date"] = date_match.group()

    # --- 提取正文 ---
    body_els = page.css(NMPA_SELECTORS["body"])
    body_el = body_els[0] if body_els else None
    if body_el:
        result["body_html"] = body_el.html_content
        result["body_text"] = body_el.get_all_text(strip=True)

    # --- 提取附件链接：找正文区域内所有文件链接 ---
    if body_el:
        for a_tag in body_el.css("a"):
            href = a_tag.attrib.get("href", "")
            name = a_tag.get_all_text(strip=True) or "未命名附件"
            if href and re.search(r'\.(pdf|doc|docx|xls|xlsx|zip|wps)$', href, re.I):
                if href.startswith("/"):
                    href = "https://www.nmpa.gov.cn" + href
                result["attachment_links"].append({"name": name, "url": href})

    return result


# ======================== 药监局列表页硬解 ========================

NMPA_LIST_SELECTORS = {
    "items": "ul.news-list li a, div.list-item a",
    "next_page": "a.next, a.page-next",
}


def extract_nmpa_list(page) -> dict:
    """
    解析药监局列表页，提取所有公告链接。

    参数:
        page: scrapling 返回的页面对象

    返回:
        dict:
        {
            "links": [{"title": "公告标题", "url": "http://..."}],
            "next_page_url": "http://..." 或 None,
        }
    """
    result = {
        "links": [],
        "next_page_url": None,
    }

    # --- 提取列表项 ---
    items = page.css(NMPA_LIST_SELECTORS["items"])
    for item in items:
        href = item.attrib.get("href", "")
        title = item.get_all_text(strip=True)
        if href and title:
            result["links"].append({"title": title, "url": href})

    # --- 提取下一页 ---
    next_btns = page.css(NMPA_LIST_SELECTORS["next_page"])
    next_btn = next_btns[0] if next_btns else None
    if next_btn:
        result["next_page_url"] = next_btn.attrib.get("href")

    return result


# ======================== 扩展模板 ========================

def extract_cde_detail(page) -> dict:
    """
    CDE（药品审评中心）详情页解析 —— 待实现模板。

    【如何实现】
    1. 打开 CDE 网站，F12 查看页面结构
    2. 找到标题、日期、正文对应的 CSS 选择器
    3. 模仿上面 extract_nmpa_detail 的写法填入

    暂时返回空结果，会自动走 Slow Track 兜底。
    """
    return {
        "title": "",
        "publish_date": "",
        "body_html": "",
        "body_text": "",
        "attachment_links": [],
    }
