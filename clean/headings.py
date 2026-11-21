"""小标题规则：NMPA 页面（fetch/extractors/nmpa.py）和附件（clean/office_text.py、清洗段 clean_stage）同一套。
NMPA 审计第二轮（复查 A-D2/D7、B-D2/D4/D5/D8、C-A2/A3）。

一行是不是小标题，看编号样式、字数和看得见的格式（加粗），不看作者设的 Word 大纲级别/标题样式
（有的公告把整段正文设成大纲 1 级）：
- 编号行：第X编/章/节、“一、”“（一）”“1.”“1 总则”“1.1”“1.1.1”开头的短行（≤40 字），不含句号、分号、感叹号，
  不含逗号（加粗或问句除外）；
- 第X条：只有“第X条”或“第X条（标题）”整行才是标题；“第四条（概念界定） 市场独占期，是指……”这种标题和正文在同一行的，
  拆成标题行和正文段（split_article）；“第一条 为了……”是正文；
- 问句：“问：……”、含“？”的编号行或加粗行，≤80 字；
- 加粗（或 Word 标题样式）的不带编号的短行（≤30 字、没有标点）；
- “附件1”“附件2 严重缺陷(举例)”这类分隔行是标题（同名的“一、厂房”分得出属于哪个附件）；
  “附件：1.… 2.…”目录里的行从来不是标题（调用方按 listing 跳过）；目录（TOC）行、页码行不是标题。
级别按编号样式在全文里的从属关系定（assign_levels）：附件分隔 > 无编号标题 > 编 > 章 > 节 > 条 > 一、 > 问句 > （一）
> 1. > 1.1 > 1.1.1；文中出现了哪几种就依次排成 ##、###……（# 留给文件标题）。

第三轮审计（2026-11-21）：
- 居中的短行（页面 text-align:center，或用全角空格缩进到中间）是标题，不要求加粗，可带括号（页面 D4）；
- “（三）联系方式：马 静 010-……”这种“字段：内容”的行不是标题（加粗的除外，页面 D2）；以“：”结尾的编号标题，
  前后同级的编号行都是正文时是引出清单的导语，不是标题（附件 D7；同级都是标题的“一、警示语应包含：”照旧）；
  “请仔细阅读说明书……”这种以“请/务必/注意”开头的提示句不是标题（附件 D7）；
- “附件：1.… 2.…”清单里的行即使 Word 里已标成标题也不是标题（附件 D4）；
- 星号只在可能成对（同一行、同一格里两个以上）或行首“* ”时转义（N2）；
- 上下标（scripted）：都有 Unicode 上下标字符的（数字、+、−、=、括号）换成 ¹²³/₁₂₃，否则写成 ^(…)/_(…)
  （和文献清洗 clean_text._mark_scripts 同一写法，N4）。
"""

from __future__ import annotations

import re

_CN = "一二三四五六七八九十百零〇两"
RANKS = ("sep", "u", "bian", "zhang", "jie", "tiao", "cn", "q", "pcn", "d1", "d2", "d3")
_NUMBERED = (
    ("bian", re.compile(rf"第[{_CN}\d]{{1,4}}编(?:\s|$)")),
    ("zhang", re.compile(rf"第[{_CN}\d]{{1,4}}章(?:\s|$)")),
    ("jie", re.compile(rf"第[{_CN}\d]{{1,4}}节(?:\s|$)")),
    ("cn", re.compile(r"[一二三四五六七八九十]{1,3}、")),
    ("pcn", re.compile(r"[（(][一二三四五六七八九十]{1,3}[)）]")),
    ("d3", re.compile(r"\d{1,2}\.\d{1,2}\.\d{1,2}(?![.．\d])\s*[^\d\s.．]")),
    ("d2", re.compile(r"\d{1,2}[.．]\d{1,2}(?![.．\d])\s*[^\d\s.．%]")),
    ("d1", re.compile(r"\d{1,2}(?:[.．、](?![\d.．])|\s+(?=[^\d\s年月日号批次个家项件条倍%]))")),
)
_ARTICLE = re.compile(rf"第[{_CN}\d]{{1,4}}条")
_ARTICLE_TITLE = re.compile(rf"(第[{_CN}\d]{{1,4}}条\s*[（(][^）)]{{1,20}}[)）])\s*(\S.*)")
ARTICLE_ONLY = re.compile(rf"第[{_CN}\d]{{1,4}}条(?:\s*[（(][^）)]{{1,20}}[)）])?")
_SEPARATOR = re.compile(r"附\s*[件表]?\s*(?:[0-9]{1,2}|[一二三四五六七八九十]{1,3})(?:\s*[：:]?\s*([^：:]{1,40}))?")
# “附件：1.… 2.…”“附：1.…”“附表：”清单的开头一行：它和后面的编号行都不是标题
LISTING_START = re.compile(r"附\s*[件表]?\s*[：:]")
_TOC_TITLE = re.compile(r"目\s*录")
_BODY_PUNCT = re.compile(r"[。；;！!]")
_COMMA = re.compile(r"[，,]")
_TOC = re.compile(r"(?:…{2,}|\.{4,}|·{4,}|-{4,})\s*\d{1,3}$")
_QUESTION_START = re.compile(r"问\s*[：:]")
_FIELD = re.compile(r"[^：:]{1,20}[：:]\s*\S")  # “字段：内容”（冒号后面还有字）
_NOTICE = re.compile(r"(?:请|务必|注意|严禁|禁止|必须)")  # 提示句的开头
_BARE_ATTACHMENT = re.compile(r"附\s*[件表]")
MAX_CHARS = 40
MAX_QUESTION = 80
MAX_EMPHASIZED = 120  # 整行加粗的编号标题（“十一、出口和接受委托生产出口麻醉药品……的说明”有 96 字）
MAX_PLAIN = 30


def numbered_kind(line: str) -> str | None:
    for kind, pattern in _NUMBERED:
        if pattern.match(line):
            return kind
    return "tiao" if _ARTICLE.match(line) else None


def rank(line: str, *, emphasized: bool = False, styled: bool = False, centered: bool = False) -> str | None:
    """这一行若是小标题，给出它的编号样式（RANKS 之一），否则 None。emphasized：整行加粗；styled：作者设了标题样式；
    centered：居中的行。"""
    s = " ".join((line or "").split())
    if _never(s):
        return None
    if _SEPARATOR.fullmatch(s) and not _BODY_PUNCT.search(s):
        return "sep"
    question = "？" in s or s.endswith("?") or bool(_QUESTION_START.match(s))
    if _sentence(s, question, emphasized):
        return None
    kind = numbered_kind(s)
    if kind == "tiao":
        return "tiao" if ARTICLE_ONLY.fullmatch(s) else None
    if len(s) > (MAX_EMPHASIZED if emphasized else MAX_QUESTION if question else MAX_CHARS):
        return None
    if kind:
        return kind if not _COMMA.search(s) or question or emphasized else None
    if centered and _centered_title(s):
        return "u"
    return _unnumbered(s, question, emphasized or styled)


def _sentence(s, question, emphasized) -> bool:
    """句子（带句号、分号、感叹号），或“（三）联系方式：马 静 010-……”这种“字段：内容”（加粗、问句除外）。
    以“？”结尾的编号问句（“二、《规定》要求，……提出药品注册申请。“药品注册申请”如何理解？”）前面带句号也是标题
    （第四轮 N1；字数限制照旧 MAX_QUESTION）。"""
    if question and s.endswith(("？", "?")) and numbered_kind(s) in _NUMBER_KINDS:
        return False
    return bool(_BODY_PUNCT.search(s)) or (not question and not emphasized and bool(_FIELD.match(s)))


def _centered_title(s) -> bool:
    """居中的短题名（“疫苗生产场地变更质量可比性研究技术指导原则”“严重缺陷(举例)”）：可带括号，不带逗号、冒号、
    顿号，不是日期、电话一类的长数字、提示句，不是单独的“附件”。"""
    bare = re.sub(r"[（(][^）)]{1,10}[)）]", "", s)  # 括号里的顿号不算（“XX省（区、市）药品监督管理局”）
    return len(s) <= MAX_PLAIN and not _NOTICE.match(s) and not _BARE_ATTACHMENT.fullmatch(s) \
        and not re.search(r"[，,：:、；;]|\d\s*[年月日]|\d{3,}", bare)


def _never(s) -> bool:
    """空行、表格行、已有记号的行、目录行、“附件：”清单行：一定不是小标题。"""
    # 以英文句点结尾的（参考文献条目“1.《……指导原则》.”）也不是；“目 录”也不是（第四轮：居中的“目 录”成了标题，
    # 它下面的正文小标题就成了目录的下级）
    return not s or s[0] in "|#>-*•[!" or bool(_TOC.search(s)) or bool(LISTING_START.match(s)) or s.endswith(".") \
        or bool(_TOC_TITLE.fullmatch(s.replace(" ", "")))


def _unnumbered(s, question, marked) -> str | None:
    """不带编号的行：“问：”开头的问句，或加粗（标题样式）的问句、短行。"""
    if question and (marked or _QUESTION_START.match(s)):
        return "q"
    if marked and len(s) <= MAX_PLAIN and not _NOTICE.match(s) and not re.search(r"[，,：:、；;（(]|\d\s*[年月日]", s):
        return "u"
    return None


def split_article(line: str):
    """“第四条（概念界定） 市场独占期，是指……”→ ("第四条（概念界定）", "市场独占期，是指……")；不是这种行返回 None。"""
    found = _ARTICLE_TITLE.fullmatch(" ".join((line or "").split()))
    return (found.group(1), found.group(2)) if found else None


def kind_of_heading(text: str) -> str:
    """已经是标题的一行（Word 标题样式、MinerU 的 text_level）属于哪种编号样式。"""
    s = " ".join(text.split())
    if _SEPARATOR.fullmatch(s):
        return "sep"
    kind = numbered_kind(s)
    if kind:
        return kind
    return "q" if "？" in s or _QUESTION_START.match(s) else "u"


def assign_levels(kinds, base: int = 2) -> dict:
    """文中出现的编号样式 → 标题级别（# 的个数），按 RANKS 的从属顺序依次排，最深 6。"""
    present = [k for k in RANKS if k in set(kinds)]
    return {k: min(base + i, 6) for i, k in enumerate(present)}


def mark(text: str, level: int) -> str:
    return "#" * level + " " + " ".join(text.split())


_PUA = re.compile("[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
# Word 符号字体（Wingdings/Symbol）的私用区字符里常见的项目符号和勾选框
_KNOWN_SYMBOLS = {
    "": "•", "": "•", "": "•", "": "•", "": "➢", "": "◆", "": "◆",
    "": "□", "": "□", "": "□", "": "□", "": "□",
    "": "☑", "": "☑", "": "☒", "": "☒", "": "✓",
}


def symbol(char: str) -> str:
    """一个私用区符号字符对应的可见符号：勾选框、项目符号；其余（段落标记一类）是空。"""
    return _KNOWN_SYMBOLS.get(char, "")


def strip_private_use(text: str) -> str:
    """私用区字符（U+E000–U+F8FF 和补充私用区）：已知的勾选框、项目符号换成 □ ☑ • ◆，其余去掉。"""
    if not text or not _PUA.search(text):
        return text
    out = _PUA.sub(lambda m: symbol(m.group(0)), text)
    return re.sub(r"(?m)^([•◆➢])(?=\S)", r"\1 ", re.sub(r"[ \t]+\n", "\n", out))


_MD_SPECIAL = re.compile(r"(?<!\\)\*")


_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def _escape_piece(piece: str) -> str:
    """一段字里的星号：可能成对（两个以上：“**ml……**mg”“***”）或在开头当列表记号（“* ”）才转义；单个“*”原样。"""
    if len(_MD_SPECIAL.findall(piece)) >= 2 or re.match(r"\s*\*(?:\s|$)", piece):
        return _MD_SPECIAL.sub(r"\\*", piece)
    return piece


def escape_stars(text: str) -> str:
    """星号转义（已转义的不再转义），只在会被当成加粗、斜体、列表记号的地方（N2）：按行看，表格行按格看。"""
    out = []
    for line in (text or "").split("\n"):
        if line.lstrip().startswith("|"):
            out.append("|".join(_escape_piece(cell) for cell in _CELL_SPLIT.split(line)))
        else:
            out.append(_escape_piece(line))
    return "\n".join(out)


def escape_markdown(text: str) -> str:
    """来源文字里的 Markdown 记号：会成对的星号转义（“**ml”不当加粗），行首的 # 转义（不当标题）。"""
    if not text:
        return text
    return re.sub(r"(?m)^(\s*)#", r"\1\\#", escape_stars(text))


_SUPER = dict(zip("0123456789+-−=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾"))
_SUB = dict(zip("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎"))


def scripted(text: str, kind: str) -> str:
    """上标（kind="sup"）、下标（"sub"）的字：每个字都有 Unicode 上下标的（β₂、10⁶、Ca²⁺）换成上下标字符，
    否则写成 ^(…)/_(…)（文献清洗 clean_text._mark_scripts 的写法）。"""
    s = (text or "").strip()
    table = _SUPER if kind == "sup" else _SUB
    if not s:
        return text or ""
    if all(c in table for c in s):
        return "".join(table[c] for c in s)
    return ("^(" if kind == "sup" else "_(") + s + ")"


_LIST_NUMBER = re.compile(r"\d{1,2}[.．、]")
_MD_HEADING = re.compile(r"(#{1,6})\s+(\S.*)")
_LISTING_TAIL = re.compile(r"[^\s，。；：,;:]{1,12}")  # 清单里一条折下来的短尾巴（“申报指南”），清单还没完
SEPARATOR_MIN_BODY = 200  # “附件1”分隔行后面到下一个分隔行至少这么多字才是分隔标题（否则是附件清单里的一行）


def _items(blocks, promote):
    """块 → [[文字, 编号样式或 None]]。promote：没有记号的行也按 rank 认标题（PDF、表格、MinerU 的结果）。"""
    items, listing = [], False
    for block in blocks:
        s = block.strip()
        if not s:
            continue
        found = _MD_HEADING.fullmatch(s) if "\n" not in s else None
        if found and listing and _LIST_NUMBER.match(found.group(2)):  # 清单里的“2.xxx”：Word 里标成标题也不是（附件 D4）
            items.append([found.group(2).strip(), None])
        elif found:
            items.append([found.group(2).strip(), kind_of_heading(found.group(2))])
            listing = False
        elif "\n" in s or s.startswith("|"):
            items.append([s, None])
            listing = False
        elif LISTING_START.match(s) or (listing and (_LIST_NUMBER.match(s) or (_LISTING_TAIL.fullmatch(s) and not _SEPARATOR.fullmatch(s)))):
            items.append([s, None])
            listing = True  # 清单的状态不论 promote 都要记：Word 里已标成标题的清单行靠它认出来（附件 D4）
        elif not promote:
            items.append([s, None])
            listing = False
        else:
            listing = False
            parts = split_article(s)
            items += [[parts[0], "tiao"], [parts[1], None]] if parts else [[s, rank(s)]]
    return items


def _drop_toc_headings(items):
    """“目录”下面的条目和正文里的小标题同名：目录里的那一份不是标题（目录里有的行没有目录样式，会被认成标题）。"""
    start = next((i for i, (t, k) in enumerate(items) if _TOC_TITLE.fullmatch(t.replace(" ", ""))), None)
    if start is None:
        return
    later = {}
    for i, (t, k) in enumerate(items):
        if k:
            later.setdefault(t, []).append(i)
    for t, places in later.items():
        for i in places[:-1]:
            if i > start:
                items[i][1] = None


_NUMBER_KINDS = ("cn", "pcn", "d1", "d2", "d3")


def _sibling(items, i, step):
    """第 i 项前（step=-1）或后（step=1）最近的同级编号行（标题或正文都算）；隔着上一级的标题就没有。"""
    kind, j = items[i][1], i + step
    while 0 <= j < len(items):
        text, k = items[j]
        if k and RANKS.index(k) < RANKS.index(kind):
            return None
        if numbered_kind(" ".join(text.split())) == kind:
            return items[j]
        j += step
    return None


def _drop_lead_ins(items):
    """以“：”结尾的编号标题，前后同级的编号行都是正文：它是引出清单的导语（“（二）……属于下列品种之一：”），不是标题。"""
    for i, (text, kind) in enumerate(items):
        if kind in _NUMBER_KINDS and text.rstrip().endswith(("：", ":")):
            near = [x for x in (_sibling(items, i, -1), _sibling(items, i, 1)) if x]
            if near and not any(k for _, k in near):
                items[i][1] = None


def _title_level(items):
    """文中有“附件N”分隔时：分隔行后面紧跟的无编号题名并进分隔行（“附件1 严重缺陷(举例)”）；第一个分隔行之前、
    任何编号标题之前的无编号题名（正文的文件题名“药品生产现场检查风险评定指导原则”）和分隔行同级——它和各附件是
    并列的，不是附件的下级（第四轮 N3；没有分隔行的同类页面本来就是 ## 题名）。"""
    seps = [i for i, (_, k) in enumerate(items) if k == "sep"]
    if not seps:
        return items
    for i in range(seps[0]):
        kind = items[i][1]
        if kind and kind != "u":
            break
        if kind == "u":
            items[i][1] = "sep"
    out = []
    for item in items:
        prev = out[-1] if out else None
        if item[1] == "u" and prev and prev[1] == "sep" and _SEPARATOR.fullmatch(prev[0]) \
                and not (_SEPARATOR.fullmatch(prev[0]).group(1) or "") \
                and len(prev[0]) + len(item[0]) < MAX_CHARS:
            prev[0] = prev[0] + " " + item[0]
            continue
        out.append(item)
    return out


_TOC_LEADER = re.compile(r"\s*(?:…{2,}|\.{4,}|·{4,}|-{4,})\s*\d{1,3}$")


def relevel(markdown: str, *, promote: bool = False, base: int = 2) -> str:
    """整篇的小标题按编号样式重排级别（多页文章各页、附件各部分用同一套级别）。开头的“# 标题”保留为第 1 级。
    附件分隔行后面正文太少的不当标题；文末没有内容的标题退成普通段落。"""
    blocks = (markdown or "").split("\n\n")
    title = blocks[0] if blocks and re.match(r"# (?!#)", blocks[0]) and "\n" not in blocks[0] else None
    items = _items(blocks[1:] if title else blocks, promote)
    for i, item in enumerate(items):
        if item[1] == "sep":
            size, j = 0, i + 1
            while j < len(items) and items[j][1] != "sep":
                size, j = size + len(items[j][0]), j + 1
            if size < SEPARATOR_MIN_BODY:
                item[1] = None
    for item in reversed(items):
        if not item[1]:
            break
        item[1] = None
    _drop_toc_headings(items)
    _drop_lead_ins(items)
    items = _title_level(items)
    levels = assign_levels([k for _, k in items if k], base)
    # 目录行的点线和页码去掉（“1、有效性外推（如适用）……9”→“1、有效性外推（如适用）”，第四轮 D-B）
    out = ([title] if title else []) + [mark(t, levels[k]) if k else _TOC_LEADER.sub("", t) if "\n" not in t else t
                                        for t, k in items]
    return "\n\n".join(out)
