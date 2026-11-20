"""国家药品监督管理局（NMPA）详情页特解

详情页的正文框 div.wenzhang 里依次是：信息表 div.wenzhang-table（索引号、主题分类、标题、发布日期）、h2.title（标题）、
h3.two-title（副标题或文号，常为空）、div.share（分享按钮）、div.date（“发布时间：…”）、div.text-bottom（法规的
“历史沿革”导航）、div.text（正文和附件链接）、div.about（【相关链接】）、div.page（翻页样式）。2026-11-20 对库里 448 个
本站页面逐个看过，都是这个结构（有的没有信息表或相关链接）。
正文只取副标题和 div.text：标题、日期另有字段；信息表里的索引号放进 extra.index_number；分享、历史沿革、相关链接
是站点导航，不是这篇文件的内容。h2.title 里的状态标记（如“【废止】”）是文件状态，写在正文第一行。
以前 extra 里的 drug_name / manufacturer / approval_number 是在正文里找“申请人”“企业名称”后面的几个字，
抽出来的多是半句话（manufacturer=“自检工作”“受理日期”），不再抽。
"""
import re

from lxml import html as lxml_html

from fetch.extractors.dates import normalize_date
from fetch.extractors.text_tools import block_text

_INDEX_NUMBER = re.compile(r"[A-Z]{2,6}-\d{4}-\d{3,6}")
_STATUS = re.compile(r"【[^】]{1,8}】")
_PUBLISHED = re.compile(r"发布时间\s*[:：]\s*[\d\-]*")
_FILE_LINK = re.compile(r"\.(pdf|doc|docx|xls|xlsx|zip|wps)$", re.I)
_NAV = ("wenzhang-table", "share", "date", "about", "page", "text-bottom")


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

    body_els = page.css("div.wenzhang")
    if not body_els:
        return result
    box = lxml_html.fromstring(body_els[0].html_content)

    # 索引号：信息表里“索引号”后面那一格（政府信息公开的固定编号）。没有信息表就不写，不从正文里猜
    texts = [" ".join(td.text_content().split()) for td in box.xpath(".//div[contains(@class,'wenzhang-table')]//td")]
    for label, value in zip(texts, texts[1:]):
        if label == "索引号" and _INDEX_NUMBER.fullmatch(value):
            result["extra"] = {"index_number": value}
            break

    # 附件：正文框内的文件链接
    for a in box.xpath(".//a[@href]"):
        href = a.get("href", "")
        if _FILE_LINK.search(href):
            if href.startswith("/"):
                href = "https://www.nmpa.gov.cn" + href
            result["attachment_links"].append({"name": " ".join(a.text_content().split()) or "未命名附件",
                                               "url": href})

    result["body_text"] = _body_text(box)
    # 正文框在、却没有字（“图解政策”这类整页是图片）：如实是空正文，不交通用兜底——兜底会把整页导航当正文
    result["page_has_no_text"] = not result["body_text"]
    return result


def _has_class(name):
    return f"contains(concat(' ',normalize-space(@class),' '),' {name} ')"


def _body_text(box) -> str:
    """正文：状态标记、副标题、正文区 div.text（没有 div.text 的页面取整个正文框，去掉上面列的导航块）。
    正文里的表格（旧版页面常见）写成 Markdown 表格，一行一条记录，不再一格一行。"""
    lines = []
    for title in box.xpath(".//h2[" + _has_class("title") + "]"):
        lines.extend(_STATUS.findall(title.text_content()))
    for sub in box.xpath(".//h3[" + _has_class("two-title") + "]"):
        text = " ".join(sub.text_content().split())
        if text:
            lines.append(text)
    for date in box.xpath(".//div[" + _has_class("date") + "]"):  # 日期栏里转载的“文章来源：…”留下，日期本身不留
        text = " ".join(_PUBLISHED.sub(" ", date.text_content()).split())
        if text:
            lines.append(text)
    parts = box.xpath(".//div[" + _has_class("text") + "]")
    if not parts:
        nav = " or ".join(_has_class(name) for name in _NAV)
        for junk in box.xpath(".//div[" + nav + "] | .//h2[" + _has_class("title") + "] | .//h3["
                              + _has_class("two-title") + "]"):
            junk.drop_tree()
        parts = [box]
    for part in parts:
        _tables_to_markdown(part)
        text = block_text(lxml_html.tostring(part, encoding="unicode"))
        if text:
            lines.append(text)
    return "\n".join(lines)


def _cell(cell) -> str:
    return " ".join(cell.text_content().replace("​", "").split()).replace("|", "｜")


def _markdown_table(rows) -> str:
    """Markdown 表格，格子不补空格：飞行检查通报这类一格一大段的表，补齐列宽会多出几百个空格和横线。"""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines[:1] + ["|" + " --- |" * width] + lines[1:])


def _tables_to_markdown(part):
    """把 part 里最外层的表格换成 Markdown 表格（每行一个 <p>，block_text 按行取）。"""
    for table in part.xpath(".//table[not(ancestor::table)]"):
        rows = [[_cell(cell) for cell in tr.xpath("./td|./th")] for tr in table.xpath(".//tr")]
        rows = [row for row in rows if any(row)]
        holder = lxml_html.Element("div")
        for line in _markdown_table(rows).split("\n") if rows else []:
            p = lxml_html.Element("p")
            p.text = line
            holder.append(p)
        holder.tail = table.tail
        table.getparent().replace(table, holder)
