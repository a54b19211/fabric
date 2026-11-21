"""Word 附件的本机读法：.docx 直读包里的 XML；.doc 先用 LibreOffice 转成 .docx 再走同一条路。

为什么要有：2026-11-17 起 MinerU 云端一直排队，.docx/.doc 只有云端一条路时 NMPA 几百个文件三次都失败。.docx 是一个
zip，正文就在 XML 里，不需要版面识别。

读法（按 document.xml 的顺序，一个段落一块、一张表一块）：
- 自动编号（NMPA 审计 A5）：段落（或它的样式）带 w:numPr 时，按 numbering.xml 的格式和计数补回编号
  （“一、”“（一）”“1.”“(1)”等；项目符号记成“- ”）；
- 标题（A5）：Heading/标题 样式或大纲级别（w:outlineLvl）的段落写成 Markdown 标题 `#`…；
- 表格（A4）：横向合并（w:gridSpan）的值填进它跨的每一列，纵向合并（w:vMerge）的值往下带，每行单独看也完整；
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

from clean.excel_parser import markdown_table
from clean.pdf_text import join_cjk

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
CONVERT_TIMEOUT = 180  # LibreOffice 转一个 .doc 的上限（秒）
_WRAPPERS = {_W + "sdt", _W + "sdtContent", _W + "customXml", _W + "smartTag", _W + "ins"}
_CN = "〇一二三四五六七八九"
_HEADING = "\x00"  # 读的过程中标题段落的前缀（后跟级别数字），docx_blocks 最后换成 #


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
        if level["fmt"] == "bullet":
            return "- "
        if level["fmt"] == "none":
            return ""

        def sub(match):
            k = int(match.group(1)) - 1
            got = counts.get(k, (levels.get(k) or {}).get("start", 1))
            return format_number(got, (levels.get(k) or {}).get("fmt", "decimal"))

        text = re.sub(r"%(\d)", sub, level["text"])
        return text if text.endswith((" ", "、", "）", ")")) or not text else text + " "


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
            self.styles[node.get(_W + "styleId")] = {
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

    def _runs(self, node, out, boxes):
        for child in node:
            tag = child.tag
            if tag == _W + "t":
                out.append(child.text or "")
            elif tag == _W + "tab":
                out.append(" ")
            elif tag in (_W + "br", _W + "cr"):
                out.append("\n")
            elif tag == _W + "txbxContent":
                boxes.append(child)
            elif tag in (_MC_FALLBACK, _W + "pPr", _W + "del"):
                continue
            elif isinstance(tag, str):
                self._runs(child, out, boxes)

    def paragraph(self, p, boxes, *, headings=True) -> str:
        out = []
        self._runs(p, out, boxes)
        lines = (" ".join(line.split()) for line in "".join(out).split("\n"))
        text = "\n".join(line for line in lines if line)
        ppr = p.find(_W + "pPr")
        style = _val(ppr, _W + "pStyle")
        num = ppr.find(_W + "numPr") if ppr is not None else None
        if num is not None and num.find(_W + "numId") is not None:
            num_id, ilvl = _val(num, _W + "numId"), _int(_val(num, _W + "ilvl"))
        else:
            num_id, ilvl = self.styles.get(style, "num") or (None, 0)
            if num is not None and num.find(_W + "ilvl") is not None:
                ilvl = _int(_val(num, _W + "ilvl"))
        label = self.numbering.label(num_id, ilvl) if text else ""
        text = label + text if text else ""
        outline = _val(ppr, _W + "outlineLvl")
        level = _int(outline) + 1 if outline is not None and _int(outline) < 9 else self.styles.get(style, "level")
        if headings and level and text and "\n" not in text and len(text) <= 80:
            return _HEADING + str(min(level, 6)) + text  # 级别先记下，整篇读完再定 # 的个数（_heading_marks）
        return text

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
        text = ""
        for child in self._flat(tc):
            if child.tag == _W + "p":
                parts = self.paragraph(child, boxes, headings=False).split("\n")
            elif child.tag == _W + "tbl":  # 格子里嵌套的表格：字算在这一格里
                parts = [" ".join(dict.fromkeys(c for c in row if c)) for row in self.rows(child, boxes)]
            else:
                continue
            for part in parts:  # 格子里分行的字接起来：中文之间不加空格（“药品\\n名称”→“药品名称”）
                text = join_cjk(text, " ".join(part.split()))
        return text

    def rows(self, tbl, boxes) -> list[list[str]]:
        grid, above = [], {}
        for tr in (x for x in self._flat(tbl) if x.tag == _W + "tr"):
            row, col = {}, _int(_val(tr.find(_W + "trPr"), _W + "gridBefore"))
            for tc in (x for x in self._flat(tr) if x.tag == _W + "tc"):
                tcpr = tc.find(_W + "tcPr")
                span = max(1, _int(_val(tcpr, _W + "gridSpan"), 1))
                merge = tcpr.find(_W + "vMerge") if tcpr is not None else None
                text = self.cell_text(tc, boxes)
                carried = merge is not None and merge.get(_W + "val") in (None, "continue")
                for k in range(col, col + span):
                    row[k] = above.get(k, "") if carried and not text else text
                col += span
            above = row
            if row:
                width = max(row) + 1
                grid.append([row.get(k, "") for k in range(width)])
        return grid

    def block(self, node, boxes) -> list[dict]:
        if node.tag == _W + "p":
            text = self.paragraph(node, boxes)
            return [{"type": "text", "text": text}] if text else []
        if node.tag == _W + "tbl":
            table = markdown_table(self.rows(node, boxes))
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
    """标题级别按在文中第一次出现的先后排成 1、2、3…：有的公告标题用“标题 2”、下面的“一、”用大纲 1 级，
    照搬会是 ## 在 # 上面。"""
    order = []
    for b in blocks:
        text = b.get("text") or ""
        if text.startswith(_HEADING) and text[1] not in order:
            order.append(text[1])
    for b in blocks:
        text = b.get("text") or ""
        if text.startswith(_HEADING):
            b["text"] = "#" * (order.index(text[1]) + 1) + " " + text[2:]
    return blocks


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
