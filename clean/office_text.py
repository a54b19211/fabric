"""Word 附件的本机读法：.docx 直读包里的 XML；.doc 先用 LibreOffice 转成 .docx 再走同一条路。

为什么要有：2026-11-17 起 MinerU 云端一直排队，.docx/.doc 只有云端一条路时 NMPA 几百个文件三次都失败。.docx 是一个
zip，正文就在 XML 里，不需要版面识别。

读法（按 document.xml 的顺序，一个段落一块、一张表一块）：
- 自动编号（NMPA 审计 A5）：段落（或它的样式）带 w:numPr 时，按 numbering.xml 的格式和计数补回编号
  （“一、”“（一）”“1.”“(1)”等；项目符号记成“- ”）；
- 标题（第二轮审计 B-D4/D5/D8、C-A2/A3）：按 clean/headings.py 的规则（和网页正文同一套）——编号短行、整段加粗的
  问句和短行、“附件1”分隔行、“第X条（标题）”；Word 的大纲级别、标题样式只当作“这行可能是标题”的一个信号，
  不单凭它定（有的公告把整段正文设成大纲 1 级）；目录（TOC 样式）的行不是标题；级别按编号样式排，不按作者设的级别；
  “第四条（概念界定） 市场独占期，是指……”这种标题和正文在一段里的（或段首“第X条”加粗的），拆成标题和正文两段；
- 表格（A4，第二轮 A-D3）：纵向合并（w:vMerge）的值往下带，每行单独看也完整；横向合并（w:gridSpan）的值只在第一格，
  跨过的格空着；一格里的几段用 <br> 隔开；第一行是表头行（tblHeader）或整行加粗才一定当表头，否则按格子像不像栏目名看；
- 符号：w:sym 和私用区字符（Wingdings 勾选框、项目符号）换成 □ ☑ • ◆，其余私用区字符去掉（段落标记一类）；
  来源文字里的 * 转义（“**ml”不当加粗）；
  整行只有一个值的开头几行（表名）写成表格前的一行字；表格用 clean.excel_parser.markdown_table，不补空格；
- 文本框（A8）：文本框里的字单独成块放在所在段落/表格后面，不并进表格行；Word 为兼容旧版本在 mc:Fallback 里
  另存的一份同样的文本框不读（以前因此重复 2 次）；批注在 comments.xml 里，不读；
- 内容控件（w:sdt）、w:customXml 包着的段落、表格、行、格照读（以前包在内容控件里的格会丢）；
- 修订：按“接受全部修订”的样子读，修订里删掉的字（w:del / w:delText）不读；
- w:tab 记成空格，w:br 记成换行。页眉页脚不读。没有页码：块定位用第几块（locator_kind=block）。
reference_chars：document.xml 里全部文字（w:t）的字数，解析结果的完整性对照（clean.attachment_parse.incomplete）。
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from clean import headings
from clean.excel_parser import SPAN, markdown_table
from clean.pdf_text import join_cjk

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
CONVERT_TIMEOUT = 180  # LibreOffice 转一个 .doc 的上限（秒）
_WRAPPERS = {_W + "sdt", _W + "sdtContent", _W + "customXml", _W + "smartTag", _W + "ins"}
_CN = "〇一二三四五六七八九"
_HEADING = "\x00"  # 读的过程中标题段落的前缀（后跟编号样式和 \x01），docx_blocks 最后换成 #


def _val(node, path):
    found = node.find(path) if node is not None else None
    return found.get(_W + "val") if found is not None else None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _chinese(n: int) -> str:
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return ("" if tens == 1 else _CN[tens]) + "十" + (_CN[ones] if ones else "")
    return str(n)


def _roman(n: int) -> str:
    out = ""
    for value, sign in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),
                        (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            out, n = out + sign, n - value
    return out


def _letter(n: int) -> str:
    out = ""
    while n > 0:
        n, rest = divmod(n - 1, 26)
        out = chr(ord("a") + rest) + out
    return out


def format_number(n: int, fmt: str) -> str:
    if fmt in ("chineseCounting", "chineseCountingThousand", "chineseLegalSimplified", "ideographTraditional",
               "japaneseCounting", "taiwaneseCounting", "taiwaneseCountingThousand"):
        return _chinese(n)
    if fmt == "decimalEnclosedCircle" or fmt == "decimalEnclosedCircleChinese":
        return chr(0x2460 + n - 1) if 1 <= n <= 20 else str(n)
    if fmt == "lowerLetter":
        return _letter(n)
    if fmt == "upperLetter":
        return _letter(n).upper()
    if fmt == "lowerRoman":
        return _roman(n).lower()
    if fmt == "upperRoman":
        return _roman(n)
    return str(n)


class _Numbering:
    """numbering.xml：numId → 各级（格式、文字模板、起始号），按出现顺序计数。"""

    def __init__(self, root):
        self.abstract, self.nums, self.counters = {}, {}, {}
        if root is None:
            return
        for node in root.findall(_W + "abstractNum"):
            levels = {}
            for lvl in node.findall(_W + "lvl"):
                levels[_int(lvl.get(_W + "ilvl"))] = {
                    "fmt": _val(lvl, _W + "numFmt") or "decimal",
                    "text": _val(lvl, _W + "lvlText") or "",
                    "start": _int(_val(lvl, _W + "start"), 1),
                }
            self.abstract[node.get(_W + "abstractNumId")] = levels
        for node in root.findall(_W + "num"):
            starts = {_int(o.get(_W + "ilvl")): _int(_val(o, _W + "startOverride"), None)
                      for o in node.findall(_W + "lvlOverride") if o.find(_W + "startOverride") is not None}
            self.nums[node.get(_W + "numId")] = (_val(node, _W + "abstractNumId"), starts)

    def label(self, num_id, ilvl) -> str:
        if not num_id or num_id == "0" or num_id not in self.nums:
            return ""
        abstract_id, starts = self.nums[num_id]
        levels = self.abstract.get(abstract_id) or {}
        if ilvl not in levels:
            return ""
        counts = self.counters.setdefault(num_id, {})
        start = starts.get(ilvl) or levels[ilvl]["start"]
        counts[ilvl] = counts[ilvl] + 1 if ilvl in counts else start
        for deeper in [k for k in counts if k > ilvl]:
            del counts[deeper]
        level = levels[ilvl]
        if level["fmt"] == "bullet":  # 勾选框、项目符号：照原样的符号（Wingdings 私用区字符换成 □ ☑ •）
            mark = level["text"][:1]
            mark = headings.symbol(mark) if "\ue000" <= mark <= "\uf8ff" else mark
            return (mark or "•") + " "
        if level["fmt"] == "none":
            return ""

        def sub(match):
            k = int(match.group(1)) - 1
            got = counts.get(k, (levels.get(k) or {}).get("start", 1))
            return format_number(got, (levels.get(k) or {}).get("fmt", "decimal"))

        text = re.sub(r"%(\d)", sub, level["text"])
        return text if text.endswith((" ", "、", "）", ")")) or not text else text + " "


def _on(node) -> bool:
    return node is not None and node.get(_W + "val") not in ("0", "false", "off")


class _Styles:
    """styles.xml：样式 → 标题级别、编号（沿 basedOn 往上找）。"""

    def __init__(self, root):
        self.styles = {}
        if root is None:
            return
        for node in root.findall(_W + "style"):
            ppr = node.find(_W + "pPr")
            name = (_val(node, _W + "name") or "").lower()
            heading = re.fullmatch(r"(?:heading|标题)\s*([1-9])", name)
            outline = _val(ppr, _W + "outlineLvl")
            num = ppr.find(_W + "numPr") if ppr is not None else None
            rpr = node.find(_W + "rPr")
            self.styles[node.get(_W + "styleId")] = {
                "name": name or None,
                "bold": _on(rpr.find(_W + "b")) if rpr is not None and rpr.find(_W + "b") is not None else None,
                "based": _val(node, _W + "basedOn"),
                "level": int(heading.group(1)) if heading else (_int(outline) + 1 if outline and _int(outline) < 9
                                                                else None),
                "num": (_val(num, _W + "numId"), _int(_val(num, _W + "ilvl"))) if num is not None else None,
            }

    def get(self, style_id, key):
        seen = set()
        while style_id and style_id in self.styles and style_id not in seen:
            seen.add(style_id)
            value = self.styles[style_id][key]
            if value is not None:
                return value
            style_id = self.styles[style_id]["based"]
        return None


class _Reader:
    def __init__(self, numbering, styles):
        self.numbering, self.styles = numbering, styles

    def _bold(self, run, inherited):
        rpr = run.find(_W + "rPr")
        b = rpr.find(_W + "b") if rpr is not None else None
        if b is not None:
            return _on(b)
        style = self.styles.get(_val(rpr, _W + "rStyle"), "bold") if rpr is not None else None
        return inherited if style is None else style

    def _runs(self, node, out, boxes, bold=False):
        """段落里的字 → out [(字, 是否加粗)]。"""
        for child in node:
            tag = child.tag
            if tag == _W + "t":
                out.append((child.text or "", bold))
            elif tag == _W + "tab":
                out.append((" ", bold))
            elif tag in (_W + "br", _W + "cr"):
                out.append(("\n", bold))
            elif tag == _W + "sym":
                out.append((headings.symbol(chr(int(child.get(_W + "char") or "0", 16) | 0xF000)), bold))
            elif tag == _W + "txbxContent":
                boxes.append(child)
            elif tag in (_MC_FALLBACK, _W + "pPr", _W + "del", _W + "rPr"):
                continue
            elif tag == _W + "r":
                self._runs(child, out, boxes, self._bold(child, bold))
            elif isinstance(tag, str):
                self._runs(child, out, boxes, bold)

    def _label(self, ppr, style, text) -> str:
        """段落的自动编号（段落自己的 numPr，或样式里的编号）。"""
        num = ppr.find(_W + "numPr") if ppr is not None else None
        if num is not None and num.find(_W + "numId") is not None:
            num_id, ilvl = _val(num, _W + "numId"), _int(_val(num, _W + "ilvl"))
        else:
            num_id, ilvl = self.styles.get(style, "num") or (None, 0)
            if num is not None and num.find(_W + "ilvl") is not None:
                ilvl = _int(_val(num, _W + "ilvl"))
        return self.numbering.label(num_id, ilvl) if text else ""

    def paragraph(self, p, boxes, *, headings=True) -> str:
        ppr = p.find(_W + "pPr")
        style = _val(ppr, _W + "pStyle")
        out = []
        self._runs(p, out, boxes, bool(self.styles.get(style, "bold")))
        raw = headings_strip("".join(t for t, _ in out))
        lines = (" ".join(line.split()) for line in raw.split("\n"))
        text = "\n".join(line for line in lines if line)
        label = self._label(ppr, style, text)
        text = label + text if text else ""
        if not headings or not text or "\n" in text or (self.styles.get(style, "name") or "").startswith("toc"):
            return headings_escape(text)
        return self._heading_or_text(text, ppr, style, out, label)

    def _heading_or_text(self, text, ppr, style, pieces, label) -> str:
        """按 headings 的规则：标题段落记下编号样式（_heading_marks 再定级别），“第X条（标题） 正文”拆成两段。"""
        outline = _val(ppr, _W + "outlineLvl")
        styled = bool(self.styles.get(style, "level")) or (outline is not None and _int(outline) < 9)
        plain = "".join(t for t, b in pieces if t.strip() and not b)
        kind = headings_rank(text, emphasized=not "".join(plain.split()), styled=styled)
        if kind:
            return _HEADING + kind + "\x01" + text
        article = headings_split(text) or _bold_article(text, pieces, label)
        if article:
            return _HEADING + "tiao\x01" + article[0] + "\n\n" + headings_escape(article[1])
        return headings_escape(text)

    def boxes(self, boxes) -> list[dict]:
        out = []
        for box in boxes:
            inner = []
            for child in self._flat(box):
                out += self.block(child, inner)
            out += self.boxes(inner)
        return out

    def _flat(self, node):
        for child in node:
            if child.tag in _WRAPPERS:
                yield from self._flat(child)
            else:
                yield child

    def cell_text(self, tc, boxes) -> str:
        """一格的字：一段一段用 <br> 隔开（“单位/企业名称”“填写人”两段不粘成一串）；一段里的换行（w:br）接起来，
        中文之间不加空格（“药品\\n名称”→“药品名称”）。"""
        paragraphs = []
        for child in self._flat(tc):
            if child.tag == _W + "p":
                text = ""
                for part in self.paragraph(child, boxes, headings=False).split("\n"):
                    text = join_cjk(text, " ".join(part.split()))
                paragraphs.append(text)
            elif child.tag == _W + "tbl":  # 格子里嵌套的表格：字算在这一格里
                paragraphs += [" ".join(dict.fromkeys(_c for _c in row if _c and _c != SPAN))
                               for row in self.rows(child, boxes)[0]]
        return _join_cell(x for x in paragraphs if x)

    def _all_bold(self, tc) -> bool:
        pieces = []
        for p in tc.iter(_W + "p"):
            self._runs(p, pieces, [], bool(self.styles.get(_val(p.find(_W + "pPr"), _W + "pStyle"), "bold")))
        texts = [(t, b) for t, b in pieces if t.strip()]
        return bool(texts) and all(b for _, b in texts)

    def _row(self, tr, above, boxes):
        """一行 → ({列号: 字}, 这一行的格子)。纵向合并的续格取上一行同列的字；横向合并跨过的列记 SPAN。"""
        row, col, cells = {}, _int(_val(tr.find(_W + "trPr"), _W + "gridBefore")), []
        for tc in (x for x in self._flat(tr) if x.tag == _W + "tc"):
            tcpr = tc.find(_W + "tcPr")
            span = max(1, _int(_val(tcpr, _W + "gridSpan"), 1))
            merge = tcpr.find(_W + "vMerge") if tcpr is not None else None
            text = self.cell_text(tc, boxes)
            carried = merge is not None and merge.get(_W + "val") in (None, "continue")
            row[col] = above.get(col, "") if carried and not text else text
            for k in range(col + 1, col + span):
                row[k] = SPAN
            col += span
            cells.append(tc)
        return row, cells

    def _header_row(self, tr, cells) -> bool:
        """第一行一定是表头：Word 标了“标题行重复”（tblHeader），或有字的格子全是加粗。"""
        trpr = tr.find(_W + "trPr")
        if trpr is not None and trpr.find(_W + "tblHeader") is not None:
            return True
        filled = [tc for tc in cells if "".join(self.cell_text(tc, []).split())]
        return bool(filled) and all(self._all_bold(tc) for tc in filled)

    def rows(self, tbl, boxes):
        """表格 → (行列表, 第一行一定是表头)。纵向合并往下带；横向合并只在第一格写值，跨过的格记 SPAN。"""
        grid, above, header = [], {}, None
        for tr in (x for x in self._flat(tbl) if x.tag == _W + "tr"):
            row, cells = self._row(tr, above, boxes)
            if header is None and row:
                header = self._header_row(tr, cells)
            above = row
            if row:
                grid.append([row.get(k, "") for k in range(max(row) + 1)])
        return grid, bool(header)

    def block(self, node, boxes) -> list[dict]:
        if node.tag == _W + "p":
            text = self.paragraph(node, boxes)
            return [{"type": "text", "text": text}] if text else []
        if node.tag == _W + "tbl":
            rows, header = self.rows(node, boxes)
            table = markdown_table([[headings_escape(c) if c != SPAN else c for c in r] for r in rows],
                                   header=True if header else None)
            return [{"type": "table", "table_body": table}] if table else []
        return []


def _xml(package, name):
    try:
        return etree.fromstring(package.read(name))
    except KeyError:
        return None


def reference_chars(root) -> int:
    """document.xml 里全部文字的字数（不计空白；mc:Fallback 里的兼容副本不算）。"""
    total = 0
    for node in root.iter(_W + "t"):
        if not any(a.tag == _MC_FALLBACK for a in node.iterancestors()):
            total += len("".join((node.text or "").split()))
    return total


def docx_blocks(path: str) -> tuple[list[dict], int]:
    """.docx → (MinerU content_list 一样的块 [{"type": "text"|"table", ...}], reference_chars)。"""
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
        reader = _Reader(_Numbering(_xml(package, "word/numbering.xml")), _Styles(_xml(package, "word/styles.xml")))
    body = root.find(_W + "body")
    blocks = []
    for child in reader._flat(body if body is not None else []):
        boxes = []
        blocks += reader.block(child, boxes)
        blocks += reader.boxes(boxes)
    return _heading_marks(blocks), reference_chars(root)


def _heading_marks(blocks):
    """标题级别按编号样式在文中的从属关系排（headings.assign_levels，## 起，# 留给附件名），不按作者设的级别。"""
    kinds = [(b.get("text") or "")[1:].split("\x01", 1)[0] for b in blocks if (b.get("text") or "").startswith(_HEADING)]
    levels = headings.assign_levels(kinds)
    for b in blocks:
        text = b.get("text") or ""
        if text.startswith(_HEADING):
            kind, rest = text[1:].split("\x01", 1)
            first, _, body = rest.partition("\n\n")
            b["text"] = headings.mark(first, levels[kind]) + ("\n\n" + body if body else "")
    return blocks


_SHORT_PART = re.compile(r"[\u4e00-\u9fff]{1,5}")


def _join_cell(paragraphs) -> str:
    """格子里的几段：一般用 <br> 隔开；窄表头里拆成两段的一个词（“药品”“名称”、“不符合规定”“项目”：前一段
    不超过 5 个汉字、后一段不超过 3 个汉字）直接接上。"""
    out = ""
    for part in paragraphs:
        last = out.rsplit("<br>", 1)[-1]
        short = _SHORT_PART.fullmatch(last) and _SHORT_PART.fullmatch(part) and len(part) <= 3
        out = out + part if short or not out else out + "<br>" + part
    return out


def _bold_article(text, pieces, label):
    """段首“第X条”（或“第X条（标题）”）是加粗的、后面是不加粗的正文：拆成 (标题, 正文)。"""
    if label or not headings.ARTICLE_ONLY.match(text):
        return None
    bold = ""
    for piece, is_bold in pieces:
        if not is_bold and piece.strip():
            break
        bold += piece
    bold = " ".join(bold.split())
    if bold and headings.ARTICLE_ONLY.fullmatch(bold) and len(text) > len(bold) + 1:
        return bold, text[len(bold):].strip()
    return None


headings_rank, headings_split, headings_escape = headings.rank, headings.split_article, headings.escape_markdown
headings_strip = headings.strip_private_use


def parse_docx(path: str) -> dict:
    """解析器约定的返回：{"success", "markdown", "content_list", "error", "reference_chars"}。"""
    try:
        blocks, reference = docx_blocks(path)
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError):
        return {"success": False, "markdown": "", "error": "not_a_docx"}
    markdown = "\n\n".join(b.get("text") or b.get("table_body") or "" for b in blocks)
    return {"success": True, "markdown": markdown, "content_list": blocks, "error": "", "reference_chars": reference}


def _soffice(path: str, work: str, target: str) -> Path:
    """LibreOffice 把文件转成 target 格式放进 work；独立的用户配置目录，几个进程同时转不会抢同一个配置锁。"""
    profile = Path(work) / "lo-profile"
    subprocess.run(
        ["soffice", f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", target,
         "--outdir", work, path],
        check=True, capture_output=True, timeout=CONVERT_TIMEOUT,
    )
    out = Path(work) / (Path(path).stem + "." + target.split(":")[0])
    if not out.is_file():
        raise RuntimeError("libreoffice produced no " + target)
    return out


def doc_to_docx(path: str, work: str) -> str:
    return str(_soffice(path, work, "docx"))


def parse_doc(path: str) -> dict:
    """.doc → LibreOffice 转 .docx → parse_docx；完整性对照另用 LibreOffice 直接导出的纯文本（不经 .docx）。"""
    with tempfile.TemporaryDirectory(prefix="doc-to-docx-") as work:
        try:
            converted = doc_to_docx(path, work)
        except (subprocess.SubprocessError, OSError, RuntimeError):
            return {"success": False, "markdown": "", "error": "libreoffice_failed"}
        out = parse_docx(converted)
        try:
            plain = _soffice(path, work, "txt:Text (encoded):UTF8").read_text(encoding="utf-8", errors="replace")
            # 纯文本导出连修订里删掉的字（w:delText）也导出；正文按“接受修订”读，删掉的字从对照里减去
            out["reference_chars"] = len("".join(plain.split())) - deleted_chars(converted)
        except (subprocess.SubprocessError, OSError, RuntimeError, zipfile.BadZipFile, KeyError):
            pass  # 没有纯文本对照：仍用 .docx 里的字数
        return out


def deleted_chars(path: str) -> int:
    """修订模式下删掉的字（w:delText）的字数。"""
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    return sum(len("".join((n.text or "").split())) for n in root.iter(_W + "delText"))
