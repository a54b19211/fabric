"""国家药品监督管理局（NMPA）详情页特解

详情页的正文框 div.wenzhang 里依次是：信息表 div.wenzhang-table（索引号、主题分类、标题、发布日期）、h2.title（标题）、
h3.two-title（副标题或文号，常为空）、div.share（分享按钮）、div.date（“发布时间：…”）、div.text-bottom（法规的
“历史沿革”导航）、div.text（正文和附件链接）、div.about（【相关链接】）、div.page（翻页）。2026-11-20 对库里 448 个
本站页面逐个看过，都是这个结构（有的没有信息表或相关链接）。
正文只取副标题和 div.text：标题、日期另有字段；信息表里的索引号放进 extra.index_number；分享、历史沿革、相关链接
是站点导航，不是这篇文件的内容。h2.title 里的状态标记（如“【废止】”）是文件状态，写在正文第一行。

2026-11-21 起（审计问题清单 F1–F5、C1、C2、K1/K2）：
- 正文一段一段落，段落之间空一行（clean_text 按空行分段，知识库按空行切块）。
- 小标题按 clean/headings.py 的规则（附件也用这一套）：编号短行、整行加粗的问句和短行、“附件1”分隔行、
  “第X条（标题）”；级别按编号样式排（采集器把多页文章拼好后用 headings.relevel 统一定级）。
- 第二轮审计（2026-11-21）：表格单元格里的几段用 <br> 隔开；横向合并（colspan）只在第一格写值、跨过的格空着，
  纵向合并（rowspan）的值往下带；没有表头行的表格不把第一行数据当表头（excel_parser.markdown_table 判断）；
  正文里写着“见下图”而正文中间有图片的，extra.content_in_media = "partial"（图里有内容、没识别）；
  信息表里的“主题分类”写进 extra.subject_category。来源文字里的 * 转义。
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

from clean import headings
from clean.clean_text import group_pipe_tables
from clean.excel_parser import markdown_table
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
_LIST_NUMBER = re.compile(r"\d{1,2}[.．、]")
_BOLD = "\u2063B\u2063"  # 整段加粗的段落在 block_text 之前打的记号（行首），_part_blocks 里取走
_BOLD_XPATH = (".//strong[not(ancestor::strong)] | .//b[not(ancestor::b)] | .//*[contains(translate(@style,' ',''),"
               "'font-weight:bold') or contains(translate(@style,' ',''),'font-weight:700')]")
_SEE_FIGURE = re.compile(r"(?:见|如)(?:下|以下|附)图|图\s*\d+\s*所示|流程图如下|示意图如下")
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

    extra = _info_table(box)
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
    elif media.get("images") and _SEE_FIGURE.search(result["body_text"]):
        extra["content_in_media"] = "partial"  # 正文说“见下图”：图里有正文内容，没有识别（图片地址在 media）
    result["extra"] = extra
    return result


def _info_table(box) -> dict:
    """信息表：“索引号”后面那一格（政府信息公开的固定编号）、“主题分类”后面那一格。没有信息表就不写，不从正文里猜。"""
    out = {}
    texts = [" ".join(td.text_content().split()) for td in box.xpath(".//div[contains(@class,'wenzhang-table')]//td")]
    for label, value in zip(texts, texts[1:]):
        if label == "索引号" and _INDEX_NUMBER.fullmatch(value):
            out["index_number"] = value
        elif label == "主题分类" and value and value not in ("标题", "索引号", "发布日期"):
            out["subject_category"] = value
    return out


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


# 单页里先给的级别；采集器拼好整篇后 headings.relevel 按全文出现的编号样式重排
_DEFAULT_LEVELS = headings.assign_levels(headings.RANKS)


def heading(line: str, emphasized: bool = False) -> str:
    """一行是小标题就加上 Markdown 标题记号，否则原样返回（* 转义）。"""
    kind = headings.rank(line, emphasized=emphasized)
    return headings.mark(line, _DEFAULT_LEVELS[kind]) if kind else headings.escape_markdown(line)


def _mark_bold(part):
    """整段加粗（<strong>/<b>/font-weight:bold 包住了全部文字）的段落：行首打记号，判标题时当作“加粗”。"""
    for node in part.xpath(".//*[self::p or self::div or self::li or self::h3 or self::h4 or self::h5]"
                           "[not(ancestor::table)][not(.//p or .//div or .//table)]"):
        full = "".join(node.text_content().split())
        bold = "".join("".join(b.text_content().split()) for b in node.xpath(_BOLD_XPATH))
        if full and (len(bold) >= len(full) or node.tag in ("h3", "h4", "h5")):
            node.text = _BOLD + (node.text or "")


def _header_blocks(box):
    """正文开头：h2 标题里的状态标记（【废止】）、副标题（文号）、日期栏里转载的“文章来源：…”（日期本身不留）。"""
    blocks = []
    for title in box.xpath(".//h2[" + _has_class("title") + "]"):
        blocks.extend(_STATUS.findall(title.text_content()))
    for sub in box.xpath(".//h3[" + _has_class("two-title") + "]"):
        blocks.append(" ".join(sub.text_content().split()))
    for date in box.xpath(".//div[" + _has_class("date") + "]"):
        blocks.append(" ".join(_PUBLISHED.sub(" ", date.text_content()).split()))
    return blocks


def _text_parts(box):
    """正文区 div.text（嵌套的只算一次）；没有 div.text 的页面取整个正文框，去掉导航块。"""
    parts = box.xpath(".//div[" + _has_class("text") + "]")
    if parts:
        return [p for p in parts if not any(p in q.iterdescendants() for q in parts)]
    nav = " or ".join(_has_class(name) for name in _NAV)
    for junk in box.xpath(".//div[" + nav + "] | .//h2[" + _has_class("title") + "] | .//h3["
                          + _has_class("two-title") + "]"):
        junk.drop_tree()
    return [box]


def _part_blocks(part, attachment_names):
    """一个正文区的块：一段一块，小标题加记号，表格整块（分组）。“附件：1.… 2.…”目录里的编号行不是小标题。"""
    blocks, listing = [], False
    tables = _tables_to_placeholders(part)
    _mark_bold(part)
    for line in block_text(lxml_html.tostring(part, encoding="unicode")).split("\n"):
        emphasized = line.strip().startswith(_BOLD)
        line = line.replace(_BOLD, "").strip()
        if not line or line in attachment_names:
            continue
        found = re.fullmatch("⁣TABLE(\\d+)⁣", line)
        if found:
            blocks.append(tables[int(found.group(1))])
            listing = False
            continue
        listing = bool(headings.LISTING_START.match(line)) or (listing and bool(_LIST_NUMBER.match(line)))
        article = None if listing else headings.split_article(line)
        if article:
            blocks += [headings.mark(article[0], _DEFAULT_LEVELS["tiao"]), headings.escape_markdown(article[1])]
        else:
            blocks.append(headings.escape_markdown(line) if listing else heading(line, emphasized))
    return blocks


def _body_text(box, attachment_names=frozenset(), header=True) -> str:
    """正文：开头的状态标记、副标题，再是正文区。块之间空一行。"""
    blocks = _header_blocks(box) if header else []
    for part in _text_parts(box):
        blocks.extend(_part_blocks(part, attachment_names))
    return headings.relevel("\n\n".join(b for b in blocks if b))  # 小标题级别按这一页的编号样式排


def _cell(cell) -> str:
    """一格的字：格子里的几段（<p>、<br> 分开的行）用 <br> 隔开，不粘在一起（复查 C-P1/P2）。"""
    lines = block_text(lxml_html.tostring(cell, encoding="unicode").replace("\u200b", "")).split("\n")
    return "<br>".join(headings.escape_markdown(" ".join(x.split())) for x in lines if x.strip()).replace("|", "｜")


def _span(value) -> int:
    try:
        return max(1, min(int(value or 1), 100))
    except ValueError:
        return 1


def _grid(table):
    """表格 → (行列表, 第一行是 <th>)：colspan 的值只写在第一格、跨过的格空着；rowspan 的值往下带。"""
    grid, header = {}, False
    for r, tr in enumerate(table.xpath(".//tr")):
        c = 0
        for cell in tr.xpath("./td|./th"):
            while (r, c) in grid:
                c += 1
            value, cols = _cell(cell), _span(cell.get("colspan"))
            header = header or (r == 0 and cell.tag == "th")
            for dr in range(_span(cell.get("rowspan"))):
                grid[(r + dr, c)] = value
                for dc in range(1, cols):
                    grid.setdefault((r + dr, c + dc), "")
            c += cols
    if not grid:
        return [], header
    height, width = max(r for r, _ in grid) + 1, max(c for _, c in grid) + 1
    return [[grid.get((r, c), "") for c in range(width)] for r in range(height)], header


def _markdown_table(rows, header=None) -> str:
    """Markdown 表格，格子不补空格；没有表头行的表格表头空着（markdown_table 判断）。长表格分组、每组带表头。"""
    return group_pipe_tables(markdown_table(rows, header=header))


def _tables_to_placeholders(part):
    """把 part 里最外层的表格换成占位行，返回 {序号: Markdown 表格}；block_text 之后再换回来，表格整块成一段。"""
    tables = {}
    for n, table in enumerate(part.xpath(".//table[not(ancestor::table)]")):
        rows, header = _grid(table)
        rows = [row for row in rows if any(row)]
        holder = lxml_html.Element("p")
        if rows:
            tables[n] = _markdown_table(rows, header=True if header else None)
            holder.text = "⁣TABLE%d⁣" % n
        holder.tail = table.tail
        table.getparent().replace(table, holder)
    return tables
