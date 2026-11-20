"""
站点提取器共用的文本小工具：空白、全角、去标签、标题。
日期规范在 fetch/extractors/dates.py；正文清洗只在 clean/clean_text.py。
"""

import re


def normalize_whitespace(text: str, preserve_newlines: bool = False) -> str:
    """
    规范化空白字符：处理 nbsp、全角空格、Tab、连续空白、多余换行。

    preserve_newlines=True 时保留换行（markdown 正文用），
    否则将所有连续空白折叠为单个空格（结构化字段值用）。
    """
    if not text:
        return ""
    # 统一不可见空白：nbsp、全角空格、Tab → 普通空格
    text = text.replace(" ", " ").replace("　", " ").replace("\t", " ")
    if preserve_newlines:
        # 行内连续空白 → 单个空格；连续 3+ 换行 → 双换行
        text = re.sub(r"[^\S\n]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        # 所有连续空白 → 单个空格
        text = re.sub(r"\s+", " ", text)
    return text.strip()


_FULLWIDTH_ALNUM = re.compile(r"[０-９Ａ-Ｚａ-ｚ]")


def full_to_half(text: str, alnum_only: bool = False) -> str:
    """全角字符转半角。

    alnum_only=False（默认）：全角 ASCII 区间 + 全角空格，与编号抽取用的是同一个实现
    （clean.identifiers.fold_fullwidth）；适合只关心数字、字母的场景。
    alnum_only=True：只转全角数字、全角字母和全角空格。U+FF01–FF5E 里也有中文标点（，；：！？（）），
    整段转换会把「主要目的：」改成「主要目的:」，处理中文字段值时用这一档。
    """
    if not text:
        return ""
    if alnum_only:
        return _FULLWIDTH_ALNUM.sub(lambda m: chr(ord(m.group()) - 0xFEE0), text.replace("　", " "))
    from clean.identifiers import fold_fullwidth

    return fold_fullwidth(text)


def remove_extra_whitespace(text: str) -> str:
    # 薄封装：保留换行的空白规范化
    return normalize_whitespace(text, preserve_newlines=True)


def remove_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def clean_title(title: str) -> str:
    title = remove_html_tags(title)
    title = normalize_whitespace(title)
    return title


# 块级元素：每个另起一行。行内元素（span、strong、a、font……）里的字接在同一行，不拆行
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})


def block_text(fragment_html: str) -> str:
    """一段 HTML 的文字，按块分行：一个段落一行，段落里的行内标签（加粗、链接、字体）不断行；<br> 换行。
    get_all_text(strip=True) 按文本节点分行，正文里一个加粗词、一个链接都会把一句话断成几行（“云端按语”一行、“：脉流……”另一行）。
    表格的格子各占一行：要成表的，调用方先把表格换成文字（见 fetch/extractors/nmpa.py）。"""
    from lxml import html as lxml_html

    if not fragment_html or not fragment_html.strip():
        return ""
    root = lxml_html.fromstring(fragment_html)
    lines, current = [], []

    def flush():
        line = " ".join("".join(current).split())
        if line:
            lines.append(line)
        current.clear()

    def walk(node):
        tag = node.tag.lower() if isinstance(node.tag, str) else ""
        if tag and tag not in _SKIP_TAGS:  # 脚本、样式不要；注释、处理指令（tag 不是字符串）只留它后面的字
            block = tag in _BLOCK_TAGS
            if block or tag == "br":
                flush()
            current.append(node.text or "")
            for child in node:
                walk(child)
            if block:
                flush()
        if node.tail:
            current.append(node.tail)

    walk(root)
    flush()
    return "\n".join(lines)
