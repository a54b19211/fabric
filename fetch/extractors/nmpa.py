"""国家药品监督管理局（NMPA）详情页特解

详情页的正文框 div.wenzhang 里依次是：信息表 div.wenzhang-table（索引号、主题分类、标题、发布日期）、h2.title（标题）、
h3.two-title（副标题或文号，常为空）、div.share（分享按钮）、div.date（“发布时间：…”）、div.text-bottom（法规的
“历史沿革”导航）、div.text（正文和附件链接）、div.about（【相关链接】）、div.page（翻页）。2026-11-20 对库里 448 个
本站页面逐个看过，都是这个结构（有的没有信息表或相关链接）。
正文只取副标题和 div.text：标题、日期另有字段；信息表里的索引号放进 extra.index_number；分享、历史沿革、相关链接
是站点导航，不是这篇文件的内容。h2.title 里的状态标记（如“【废止】”）是文件状态，写在正文第一行。

2026-11-21 起（审计问题清单 F1–F5、C1、C2、K1/K2）：
- 正文一段一段落，段落之间空一行（clean_text 按空行分段，知识库按空行切块）。
- “一、”“（一）”“1.”“第一章”这类短的小标题行写成 Markdown 标题（##、###、####），知识库按标题切块。
  一行带句号、或超过 40 字，是以编号开头的正文段落，不当标题。
- 表格写成 Markdown 管道表格；长表格按 TABLE_GROUP_CHARS 分成几组，每组都带表头，组之间空一行：知识库切块只会
  落在组和组之间，不会切在一行中间，切开后的每块都有表头。
- 【相关链接】（div.about，或正文区里以“【相关链接】”开头的块）一律不要。
- 附件只收看得见的：链接有文字、不在 display:none 里。没文字的下载按钮（同一文件的另一份、或整篇的打包件）不收。
  正文里只是一个附件链接的那一行（链接文字就是整行）也不留在正文里：附件另有记录。
- 多页文章（div.page 里“共N页”，子页是 <编号>_1.html、_2.html…）：continuation_links 给出子页链接，
  采集器（fetch/site_crawler.py）逐页抓、用 extract_nmpa_continuation 取正文和附件接在后面。
- 图片、视频：extra.media 记下正文区里的图片、视频地址；正文没有字、只有图片或视频的页面，
  extra.content_in_media = "image" / "video"：内容在图片或视频里，没有做识别，不是空文件。
- 文号（“国药监药注〔2024〕1号”“2026年第42号”）从副标题或标题里取，写进 extra.doc_number，附件记录继承它。
以前 extra 里的 drug_name / manufacturer / approval_number 是在正文里找“申请人”“企业名称”后面的几个字，
抽出来的多是半句话（manufacturer=“自检工作”“受理日期”），不再抽。
"""
import re
from urllib.parse import urljoin

from lxml import html as lxml_html

from clean.clean_text import group_pipe_tables
from fetch.extractors.dates import normalize_date
from fetch.extractors.text_tools import block_text

SITE = "https://www.nmpa.gov.cn"
_INDEX_NUMBER = re.compile(r"[A-Z]{2,6}-\d{4}-\d{3,6}")
_STATUS = re.compile(r"【[^】]{1,8}】")
_PUBLISHED = re.compile(r"发布时间\s*[:：]\s*[\d\-]*")
_FILE_LINK = re.compile(r"\.(pdf|doc|docx|xls|xlsx|zip|rar|wps|et|ofd)$", re.I)
_NAV = ("wenzhang-table", "share", "date", "about", "page", "text-bottom")
_RELATED = "【相关链接】"
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_SUBPAGE = re.compile(r"^(?:.*/)?(\d{12,})_(\d{1,3})\.html$")
_DOC_NUMBER = re.compile(r"[一-龥]{2,20}〔\d{4}〕\d+号|\d{4}年第\d+号")
# 小标题：编号 → 标题级别（“# 标题”是第 1 级）
_HEADINGS = (
    (re.compile(r"第[一二三四五六七八九十百零〇]+[章编](?:\s|$)"), "## "),
    (re.compile(r"第[一二三四五六七八九十百零〇]+节(?:\s|$)"), "### "),
    (re.compile(r"[一二三四五六七八九十]{1,3}、"), "## "),
    (re.compile(r"[（(][一二三四五六七八九十]{1,3}[)）]"), "### "),
    (re.compile(r"\d{1,2}[.．、](?!\d)"), "#### "),
)
HEADING_MAX_CHARS = 40
_SENTENCE = re.compile(r"[。；;！？!?]")
_ICON = re.compile(r"/wbppimages/|share_|\.gif$", re.I)


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

    extra = {}
    # 索引号：信息表里“索引号”后面那一格（政府信息公开的固定编号）。没有信息表就不写，不从正文里猜
    texts = [" ".join(td.text_content().split()) for td in box.xpath(".//div[contains(@class,'wenzhang-table')]//td")]
    for label, value in zip(texts, texts[1:]):
        if label == "索引号" and _INDEX_NUMBER.fullmatch(value):
            extra["index_number"] = value
            break
    subtitle = " ".join(" ".join(h.text_content().split()) for h in box.xpath(".//h3[" + _has_class("two-title") + "]"))
    number = _DOC_NUMBER.search(subtitle) or _DOC_NUMBER.search(result["title"])
    if number:
        extra["doc_number"] = number.group(0)

    _drop_related(box)
    result["attachment_links"] = _attachments(box)
    _drop_hidden(box)
    result["body_text"] = _body_text(box, {a["name"] for a in result["attachment_links"]})
    media = _media(box)
    if media:
        extra["media"] = media
    # 正文框在、却没有字（“图解政策”这类整页是图片，视频页）：如实是空正文，不交通用兜底——兜底会把整页导航当正文
    result["page_has_no_text"] = not result["body_text"]
    if result["page_has_no_text"] and media:
        extra["content_in_media"] = "video" if media.get("videos") else "image"
    result["extra"] = extra
    return result


def extract_nmpa_continuation(page) -> dict:
    """多页文章的子页（<编号>_N.html）：只要正文区的字和附件，标题、日期、状态标记第 1 页已经有了。"""
    body_els = page.css("div.wenzhang")
    if not body_els:
        return {"body_text": "", "attachment_links": []}
    box = lxml_html.fromstring(body_els[0].html_content)
    _drop_related(box)
    links = _attachments(box)
    _drop_hidden(box)
    return {"body_text": _body_text(box, {a["name"] for a in links}, header=False), "attachment_links": links}


def _has_class(name):
    return f"contains(concat(' ',normalize-space(@class),' '),' {name} ')"


def continuation_links(page) -> list[str]:
    """多页文章：div.page 里本篇的子页链接（<编号>_N.html），按页序、去重；相对链接原样给，采集器按页面地址补全。"""
    html = getattr(page, "html_content", "") or ""
    if "wenzhang" not in html or "_1.html" not in html:  # 单页文章（绝大多数）：不再解析
        return []
    body_els = page.css("div.wenzhang")
    if not body_els:
        return []
    box = lxml_html.fromstring(body_els[0].html_content)
    pages = {}
    for a in box.xpath(".//div[" + _has_class("page") + "]//a[@href]"):
        href = a.get("href", "").strip()
        found = _SUBPAGE.match(href)
        if found:
            pages.setdefault(int(found.group(2)), href)
    return [pages[n] for n in sorted(pages)]


def _drop_related(box):
    """【相关链接】：div.about 整块；正文区里以“【相关链接】”开头的块连同它后面的同级块（链接列表）。"""
    for about in box.xpath(".//div[" + _has_class("about") + "]"):
        about.drop_tree()
    for node in box.xpath(".//*[self::p or self::div or self::span or self::strong or self::b]"):
        if node.getparent() is None:
            continue
        if " ".join(node.text_content().split()).startswith(_RELATED):
            for later in list(node.itersiblings()):
                later.drop_tree()
            node.drop_tree()


def _hidden(node):
    return any(_HIDDEN.search(n.get("style") or "") or n.get("hidden") is not None
               for n in [node, *node.iterancestors()])


def _drop_hidden(box):
    """页面上看不见的块（display:none）里的字不算正文。"""
    for node in box.xpath(".//*[@style or @hidden]"):
        if node.getparent() is not None and (_HIDDEN.search(node.get("style") or "") or node.get("hidden") is not None):
            node.drop_tree()


def _attachments(box):
    """正文框内看得见、有文字的文件链接。"""
    links, seen = [], set()
    for a in box.xpath(".//a[@href]"):
        href = a.get("href", "").strip()
        name = " ".join(a.text_content().split())
        if not _FILE_LINK.search(href) or not name or _hidden(a):
            continue
        url = urljoin(SITE + "/", href) if not href.startswith("http") else href
        if url not in seen:
            seen.add(url)
            links.append({"name": name, "url": url})
    return links


def _media(box):
    """正文区里的图片、视频地址（站点自己的按钮图标不算）。"""
    parts = box.xpath(".//div[" + _has_class("text") + "]") or [box]
    images, videos = [], []
    for part in parts:
        for img in part.xpath(".//img[@src]"):
            src = img.get("src").strip()
            if src and not _ICON.search(src):
                images.append(urljoin(SITE + "/", src))
        for v in part.xpath(".//video[@src] | .//video/source[@src] | .//embed[@src] | .//iframe[@src]"):
            src = v.get("src").strip()
            if src:
                videos.append(urljoin(SITE + "/", src))
    out = {}
    if images:
        out["images"] = list(dict.fromkeys(images))
    if videos:
        out["videos"] = list(dict.fromkeys(videos))
    return out


def heading(line: str) -> str:
    """一行是小标题就加上 Markdown 标题记号，否则原样返回。"""
    if len(line) > HEADING_MAX_CHARS or _SENTENCE.search(line) or line.startswith(("|", "#")):
        return line
    for pattern, mark in _HEADINGS:
        if pattern.match(line):
            return mark + line
    return line


def _body_text(box, attachment_names=frozenset(), header=True) -> str:
    """正文：状态标记、副标题、正文区 div.text（没有 div.text 的页面取整个正文框，去掉上面列的导航块）。
    一段一块，块之间空一行；表格整块（分组）保留。"""
    blocks = []
    if header:
        for title in box.xpath(".//h2[" + _has_class("title") + "]"):
            blocks.extend(_STATUS.findall(title.text_content()))
        for sub in box.xpath(".//h3[" + _has_class("two-title") + "]"):
            text = " ".join(sub.text_content().split())
            if text:
                blocks.append(text)
        for date in box.xpath(".//div[" + _has_class("date") + "]"):  # 日期栏里转载的“文章来源：…”留下，日期本身不留
            text = " ".join(_PUBLISHED.sub(" ", date.text_content()).split())
            if text:
                blocks.append(text)
    parts = box.xpath(".//div[" + _has_class("text") + "]")
    if not parts:
        nav = " or ".join(_has_class(name) for name in _NAV)
        for junk in box.xpath(".//div[" + nav + "] | .//h2[" + _has_class("title") + "] | .//h3["
                              + _has_class("two-title") + "]"):
            junk.drop_tree()
        parts = [box]
    else:
        parts = [p for p in parts if not any(p in q.iterdescendants() for q in parts)]  # 嵌套的 div.text 只算一次
    for part in parts:
        tables = _tables_to_placeholders(part)
        for line in block_text(lxml_html.tostring(part, encoding="unicode")).split("\n"):
            line = line.strip()
            if not line or line in attachment_names:
                continue
            found = re.fullmatch(r"⁣TABLE(\d+)⁣", line)
            blocks.append(tables[int(found.group(1))] if found else heading(line))
    return "\n\n".join(b for b in blocks if b)


def _cell(cell) -> str:
    return " ".join(cell.text_content().replace("​", "").split()).replace("|", "｜")


def _markdown_table(rows) -> str:
    """Markdown 表格，格子不补空格：飞行检查通报这类一格一大段的表，补齐列宽会多出几百个空格和横线。
    长表格分组、每组带表头（group_pipe_tables）。"""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in rows]
    return group_pipe_tables("\n".join(lines[:1] + ["|" + " --- |" * width] + lines[1:]))


def _tables_to_placeholders(part):
    """把 part 里最外层的表格换成占位行，返回 {序号: Markdown 表格}；block_text 之后再换回来，表格整块成一段。"""
    tables = {}
    for n, table in enumerate(part.xpath(".//table[not(ancestor::table)]")):
        rows = [[_cell(cell) for cell in tr.xpath("./td|./th")] for tr in table.xpath(".//tr")]
        rows = [row for row in rows if any(row)]
        holder = lxml_html.Element("p")
        if rows:
            tables[n] = _markdown_table(rows)
            holder.text = "⁣TABLE%d⁣" % n
        holder.tail = table.tail
        table.getparent().replace(table, holder)
    return tables
