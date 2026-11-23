"""加工层：附件原件 → 带定位的正文（复用旧 ETL 的解析实现，按 MinerU 实际能力排顺序）。

11-04 实测自部署 MinerU 3.4.0（旧配置 APP.MINERU_API_BASE）：PDF、DOCX 原生解析；DOC、XLS 返回
“Unsupported file type”。MinerU 4.x 和云端 API 原生支持 DOC/PPT/XLS 等 Office 格式。所以：
- PDF / DOCX：自部署 MinerU 原生解析（clean/mineru_local.py）；失败且配置了云端 token → 云端；
- DOC / PPT / PPTX：配置了云端 token → 云端原生解析（clean.mineru_cloud）；否则 LibreOffice 转 PDF
  （clean.docx_to_pdf._doc_to_pdf，在临时目录里转）再交自部署 MinerU；
- XLS / XLSX：openpyxl / xlrd 直读成 Markdown 表格（clean.excel_parser.excel_to_markdown）；
- 扩展名未知（.bin）时按文件头判断（fetch.web_client._sniff_bytes）；
- XML（PMC 全文 JATS）：clean.jats_text 直读结构，不调外部服务；不是 JATS 文章记 unsupported。
  加 XML 不改变其他格式的结果，此前也没有 .xml 附件（download_pdf 只产出 pdf/doc/docx/xls/xlsx/bin），
  所以处理器版本不升。

定位：按格式给实际能给的定位，不强行编页码。
- 原生 PDF：MinerU content_list 的 page_idx → pdf_page；
- DOC/PPT 经转换（LibreOffice 或云端内部转换）得到的页码 → converted_page，注明不是原文件页码；
- DOCX 原生解析没有分页（11-04 实测所有块 page_idx=0）→ block（第几个内容块）；
- Excel → sheet；
- JATS → jats_section（节路径 + JATS 路径，没有页码）；
- 解析服务没给 content_list → locator_kind=unavailable，正文仍可用，但不能回指页码。
正文由 content_list 的块按顺序拼成，segments 记每块在正文里的起止偏移和定位（页码只记在 segments 里，正文里
不插页码标记，见 v4）。

大小与耗时：原件超过 MAX_PARSE_BYTES 直接记 too_large，不送解析；每种解析方式各有超时。
v2：加了定位与大小上限，结果与 v1 不同，所以升处理器版本。
v3（2026-11-20）：Word 先在本机读（clean.office_text）：.docx 直读 document.xml，.doc 用 LibreOffice 转 .docx 再读；
读不出字才交 MinerU。原因：MinerU 云端排队以后 .docx/.doc 没有别的路，NMPA 200 个 .docx、73 个 .doc 三次都失败。
DATAFABRIC_MINERU_MODE=off 时完全不调 MinerU（PDF 只读文字层）。v2 已解析成功的同一文件（sha256 相同）直接沿用 v2 的
正文（clean.derived_text._parse），不重新解析，所以 v2 的成功结果不变；变的只有 v2 失败的那些。
v4（2026-11-21，NMPA 审计 A1–A11）：
- PDF 先读文字层（pypdf），文字层可用才用（_usable_text_layer：每页平均不少于 PDF_MIN_CHARS_PER_PAGE 个字、
  读不出字的页不超过 PDF_MAX_EMPTY_PAGE_SHARE，乱码页算读不出）；不可用（扫描件）才交 MinerU（A1）；
- 正文里不再插“〔第N页〕”：以前页码标记缺页、docx 里也有（A11）；页码只记在 segments 里；
- Excel / Word 表格不补空格、只取用到的范围、合并格往下往右带值、格内换行不拆行（A2/A3/A4/A10，clean.excel_parser、
  clean.office_text）；Word 补回自动编号和标题、文本框单独成块不重复（A5/A8）；
- 完整性（A6）：解析器给了独立对照字数（reference_chars：docx 的 XML 全文、.doc 的 LibreOffice 纯文本导出）时，
  正文字数不到对照的 COMPLETE_SHARE 记 incomplete（不是 parsed），并换下一个解析器；MinerU 读 PDF 时有内容的页数
  不到 PDF 页数的 COMPLETE_SHARE 也记 incomplete；
- 所有正文过 tidy（A7）：HTML 表格转 Markdown 表格（合并格带值），去掉私用区项目符号（U+F000–U+F0FF），
  MinerU 的标题级别（text_level）写成 Markdown 标题；
- 不再沿用旧版本的结果（A9）：v2/v3 复用来的正文里有原始 HTML 表格、页码标记；同一文件（sha256）只复用 v4 自己的结果。
v5（2026-11-21，NMPA 第二轮审计）：
- Word 小标题按 clean/headings.py（和网页同一套，不单凭大纲级别）；横向合并只在第一格写值；格内几段用 <br> 隔开；
  勾选框 □ ☑ 保留；没有表头行的表格表头空着；来源里的 * 转义（clean.office_text）；
- 私用区字符（U+E000–U+F8FF、补充私用区）全部处理：已知勾选框、项目符号换成 □ ☑ • ◆，其余去掉（以前只去 U+F000–U+F0FF，
  U+E003/U+E004 段落标记留在正文里）；
- PDF：跨页、右括号结尾的折行接成一段，“少发 /罕见”“forKids”字间空格；没有字码映射的字不收、页号记在
  attempts（"pypdf_text:missing_glyphs:1,3"）（clean.pdf_text）；
- Excel：表名纵向合并只写一次，单个 Sheet 不写“## Sheet1”，横向合并只在第一格（clean.excel_parser）。
同一文件只复用 v5 自己的结果。
v6（2026-11-21，NMPA 第三轮审计）：
- PDF 缺字按画字指令数（clean.pdf_text）；有缺字的结果照样是 parsed（其余正文可用），但 error 写
  "missing_glyphs:<页号>"、attempts 记 "pypdf_text:missing_glyphs:<页号>"，清洗段据此给清洗正文加警告
  attachment_missing_glyphs（画像格式 degraded）；同 sha256 复用时也带上（clean.derived_text）。没有做 OCR；
- Word 格子里的软换行写 <br>，上下标（clean.office_text）；Excel 格内换行写 <br>；表名行只取合并成一格的，表单的
  标签行留在表里；没有表头行的表格用前几行里的栏目名行或“列1…列N”当表头（clean.excel_parser）；
- PDF 接段、➢、“/”“-”两侧空格（clean.pdf_text）。
同一文件只复用 v6 自己的结果。
v7（2026-11-21）：表格中间的栏目名行里有一个横跨两列的格（“品名举例”占两列）也认作栏目名行（v6 只认没有合并格的，
d2705923 的几张分表表头仍是上层分组行）。同一文件只复用 v7 自己的结果。
v8（2026-11-21，NMPA 第四轮审计）：
- PDF：读不出的字（贴图、没有字码映射的字体、只画了空格）所在的标记内容，结构树里有 ActualText 的用它补回（红头、
  总标题、子附件的“附件 1”和题名，D-A；补不回的照旧记 missing_glyphs）；英文参考文献折行接回、表格最后一格跨页的
  续行接回格里、Cmax 这类小字号偏下（上）的字写成下标（上标）（D-B）（clean.pdf_text）；
- Word：居中（段落或样式里的 jc=center）的无编号短行是标题，和页面同一条规则；连着两行居中题名同字号（或下一行是
  “（征求意见稿）”、上一行是发文机关）接成一个（S1）；字符样式（rStyle）里的上下标（D-C）（clean.office_text）；
- 表格：表头上面的填写行、分组行照原顺序写在表前，不挪到表头下面（D-D）；没有栏目名行时第一行当表头，不补原文没有的
  “列1 | 列2”（N2）（clean.excel_parser）。
同一文件只复用 v8 自己的结果。
v9（2026-11-21，NMPA 第五轮审计）：PDF 的 ● ■ ◆ ▪ ○ ➢ ★ 等项目符号和 • 一样认，只有一个项目符号的行和下一行接成
一条（E1，clean.pdf_text）；表头上面的分组行（“6801 基础外科手术器械 | 对应关系 | 备注”）写进表头，分块后每块都带
类别名（E3，clean.excel_parser）。同一文件只复用 v9 自己的结果。
v10（2026-11-21，复查6）：项目符号那一条的悬挂缩进续行（句号后面的续句）接回这一条（D1）；Word 里连成一张的两张分表，
后一张的分组行 + 栏目名行另起一张表、带自己的类别表头（D2）。同一文件只复用 v10 自己的结果。
v11（2026-11-23，真推知识库切块审计）：表格写出时格内分行连成一行（“；”隔开，括号、标点后直接接），表格里不留
<br>；分组名和栏目名之间用“／”隔开；分组行里的长编辑备注写在表格前面、分组名照样进表头（062004cc）；表头一格
横跨“大类名 | 序号”两列时表头名放到序号那一列（6ea7bcfb）（clean.excel_parser）。同一文件只复用 v11 自己的结果。
v12（2026-11-23，切块审计2）：格内折行直接接上，只在下一行是编号、勾选框、“字段名：”或上一行是单位名时用“；”
隔开（v11 往“国药准字；Z34020868”这种词中间插了分号）。同一文件只复用 v12 自己的结果。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

PROCESSOR_VERSION = "datafabric.attachment_parse.v12"
# 旧版本的结果不再沿用（v4–v7 改了输出，见模块说明）
REUSABLE_VERSIONS: tuple[str, ...] = ()
SELF_HOSTED_NATIVE = {".pdf", ".docx"}
CLOUD_NATIVE = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
CONVERTIBLE = {".doc", ".ppt", ".pptx"}
EXCEL_EXTS = {".xls", ".xlsx"}
SUPPORTED = SELF_HOSTED_NATIVE | CLOUD_NATIVE | EXCEL_EXTS
JATS_EXTS = {".xml"}
SKIP_BLOCKS = {"page_number", "header", "footer", "page_header", "page_footer", "discarded"}


def max_parse_bytes() -> int:
    return int(os.environ.get("DATAFABRIC_ATTACHMENT_MAX_PARSE_BYTES", str(200 * 1024 * 1024)))


def mineru_base() -> str:
    """自部署 MinerU 地址：DATAFABRIC_MINERU_URL，未配置时沿用旧配置 APP.MINERU_API_BASE。"""
    value = os.environ.get("DATAFABRIC_MINERU_URL", "")
    if not value:
        from settings import APP

        value = APP.MINERU_API_BASE
    return value.rstrip("/")


def _mineru(path: str) -> dict:
    from clean import mineru_local

    return mineru_local.parse(path, base=mineru_base(), content_list=True, timeout=300)


def _mineru_cloud(path: str) -> dict:
    from clean import mineru_cloud

    return mineru_cloud.parse(path)


def _cloud_enabled() -> bool:
    return bool(os.environ.get("DATAFABRIC_MINERU_CLOUD_TOKEN"))


def _mineru_off() -> bool:
    """DATAFABRIC_MINERU_MODE=off：不调 MinerU（自部署和云端都不调），PDF 只读文字层，Word 只用本机读法。"""
    return os.environ.get("DATAFABRIC_MINERU_MODE", "") == "off"


def _office(path: str) -> dict:
    from clean import office_text

    return office_text.parse_docx(path)


def _office_doc(path: str) -> dict:
    from clean import office_text

    return office_text.parse_doc(path)


def _cloud_only() -> bool:
    """DATAFABRIC_MINERU_MODE=cloud: PDF / DOCX go straight to MinerU cloud (no self-hosted MinerU is deployed)."""
    return os.environ.get("DATAFABRIC_MINERU_MODE", "") == "cloud" and _cloud_enabled()


def _doc_to_pdf(path: str) -> str:
    from clean.docx_to_pdf import _doc_to_pdf

    return _doc_to_pdf(path)


def _excel(path: str) -> str:
    from clean.excel_parser import excel_to_markdown

    return excel_to_markdown(path)


def is_web_page(content: bytes) -> bool:
    from fetch.web_client import is_web_page as check

    return check(content)


def _sniff(content: bytes) -> str:
    from fetch.web_client import _sniff_bytes

    return _sniff_bytes(content)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 文件不小、解析出来却几乎没有字：内容在页面图片里（扫描件），解析器没给出文字层。实测 NMPA 4–9 MB 的 .docx 只解析出
# “附件”两个字、94 KB 的 PDF 只有 6 个字；真正的短文档 14 KB 就有 120 字以上
SCAN_MIN_BYTES = 50_000
SCAN_MAX_CHARS = 20
_PAGE_MARK = re.compile(r"〔第\d+页〕|\s")


def without_text_layer(result: dict, size: int) -> dict:
    """解析成功但几乎没有字的大文件：改记为 unsupported，原因 text_layer_missing（要 OCR），不当成只有几个字的正文。"""
    if result["status"] != "parsed" or size < SCAN_MIN_BYTES \
            or len(_PAGE_MARK.sub("", result["text"])) >= SCAN_MAX_CHARS:
        return result
    return _result("unsupported", method=result["parse_method"], error="text_layer_missing", ext=result["file_ext"],
                   attempts=[*result["attempts"], "text_layer_missing"])


def _result(status, *, text="", method=None, error=None, ext=None, attempts=(), segments=None, locator_kind=None):
    return {
        "status": status,
        "text": text,
        "chars": len(text),
        "parse_method": method,
        "error": error,
        "file_ext": ext,
        "attempts": list(attempts),
        "segments": segments or [],
        "locator_kind": locator_kind or ("unavailable" if status == "parsed" else None),
        "processor_version": PROCESSOR_VERSION,
    }


def _block_text(block: dict) -> str:
    kind = block.get("type")
    if kind == "table":
        caption = " ".join(block.get("table_caption") or [])
        body = block.get("table_body") or ""
        foot = " ".join(block.get("table_footnote") or [])
        return "\n".join(x for x in (caption, body, foot) if x)
    if kind == "image":
        return " ".join(block.get("image_caption") or [])
    if kind == "list":
        return "\n".join(str(x) for x in (block.get("list_items") or []))
    body = str(block.get("text") or "")
    level = block.get("text_level")
    if isinstance(level, int) and 1 <= level <= 6 and body.strip() and not body.lstrip().startswith("#"):
        return "#" * level + " " + body.strip()
    return body


def from_blocks(blocks, page_kind) -> tuple[str, list, str]:
    """content_list → (正文, segments, locator_kind)。page_kind 为 None 表示没有可信页码，按块定位。"""
    parts, segments, offset = [], [], 0
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") in SKIP_BLOCKS:
            continue
        body = tidy(_block_text(block)).strip()
        if not body:
            continue
        page = block.get("page_idx")
        locator = {"block": index}
        if page_kind and isinstance(page, int):
            locator["page"] = page + 1
        segments.append({"start": offset, "end": offset + len(body), "type": block.get("type"), **locator})
        parts.append(body)
        offset += len(body) + 2
    return "\n\n".join(parts), segments, (page_kind or "block")


def _sheets(markdown: str) -> list:
    out = []
    for match in re.finditer(r"^## (.+)$", markdown, flags=re.M):
        out.append({"start": match.start(), "sheet": match.group(1).strip()})
    for i, seg in enumerate(out):
        seg["end"] = out[i + 1]["start"] if i + 1 < len(out) else len(markdown)
    return out or ([{"start": 0, "end": len(markdown), "sheet": None}] if markdown else [])  # 只有一个 Sheet


def _plan(ext):
    """按格式排解析顺序：[(方法名, 调用, 页码种类)]。页码种类 None 表示没有可信页码。"""
    cloud_kind = "pdf_page" if ext == ".pdf" else "converted_page"
    cloud = [("mineru_cloud", _mineru_cloud, cloud_kind)] if _cloud_enabled() else []
    if ext in EXCEL_EXTS:
        return [("excel", lambda p: {"success": True, "markdown": _excel(p), "error": ""}, None)]
    # PDF 先读文字层：可用（_usable_text_layer）就用，扫描件才交 MinerU
    local = {".docx": [("office_docx", _office, None)], ".doc": [("libreoffice_docx", _office_doc, None)],
             ".pdf": [("pypdf_text", _pdf_text_layer, "pdf_page")]}.get(ext, [])
    if _mineru_off():
        return local
    if cloud and _cloud_only() and ext in (".pdf", ".docx"):
        return local + cloud
    if ext == ".pdf":
        return local + [("mineru", _mineru, "pdf_page")] + cloud
    if ext == ".docx":
        return local + [("mineru", _mineru, None)] + cloud
    if cloud:
        return local + cloud
    return local + [("libreoffice_pdf+mineru", lambda p: _mineru(_doc_to_pdf(p)), "converted_page")]


# 文字层里字体编码表缺失的页，读出来是一串别的文字（希腊、泰卢固、古木基等字母，港交所年报常见）：
# 拉丁字母、数字、标点、中日韩文字以外的字超过两成的页当作读不出，不收乱码
_ODD = re.compile(r"[^\s\u0000-\u024f\u2000-\u206f\u2460-\u24ff\u3000-\u9fff\uff00-\uffef]")


def _readable(text: str) -> str:
    chars = sum(1 for c in text if not c.isspace())
    return text if chars and len(_ODD.findall(text)) <= 0.2 * chars else ""


# 文字层可用的标准（A1）：每页平均不少于 30 个字，读不出字（少于 10 个字或乱码）的页不超过两成。
# 扫描件（安徽局印章式样 PDF：没有文字层）、一部分页是图片的 PDF 交 MinerU；NMPA 文字版 PDF 每页几百字
PDF_MIN_CHARS_PER_PAGE = 30
PDF_EMPTY_PAGE_CHARS = 10
PDF_MAX_EMPTY_PAGE_SHARE = 0.2


def _usable_text_layer(page_texts: list[str]) -> bool:
    counts = [len("".join(t.split())) for t in page_texts]
    if not counts:
        return False
    empty = sum(1 for c in counts if c < PDF_EMPTY_PAGE_CHARS)
    return sum(counts) / len(counts) >= PDF_MIN_CHARS_PER_PAGE and empty <= PDF_MAX_EMPTY_PAGE_SHARE * len(counts)


def _pdf_text_layer(path: str) -> dict:
    """PDF 自带的文字层（pypdf），一页一块。文字层不可用（扫描件、乱码、大半是图片页）时交回失败，换 MinerU。"""
    from pypdf import PdfReader

    from clean.pdf_text import join_pages, page_reading

    pages = PdfReader(path).pages
    if not _usable_text_layer([_readable(page.extract_text() or "") for page in pages]):
        return {"success": False, "markdown": "", "error": "no_usable_text_layer"}
    readings = [page_reading(page) for page in pages]  # 表格按格读成 Markdown 表格（clean.pdf_text）
    texts = join_pages([_readable(text) for text, _ in readings])  # 跨页断开的段落接回去
    blocks = [{"type": "text", "text": t, "page_idx": index} for index, t in enumerate(texts)]
    markdown = "\n\n".join(t for t in texts if t)
    missing = [str(i + 1) for i, (_, lost) in enumerate(readings) if lost]
    return {"success": True, "markdown": markdown, "content_list": blocks, "error": "",
            **({"missing_glyph_pages": missing} if missing else {})}


def _pdf_pages(path) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001 -- 读不出页数：不做页数对照
        return 0


# tidy（A7）：HTML 表格 → Markdown，私用区字符（v5：全部私用区，已知符号换成可见符号）
_HTML_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)


def html_table_to_markdown(html: str) -> str:
    """一张 HTML 表格 → Markdown 表格；rowspan 的值往下带，colspan 只在第一格写值。"""
    from lxml import html as lxml_html

    from clean.excel_parser import SPAN, markdown_table

    try:
        root = lxml_html.fragment_fromstring(html, create_parent="div")
    except Exception:  # noqa: BLE001 -- 读不出的片段：去掉标签留字
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())
    grid: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(root.iter("tr")):
        c = 0
        for cell in tr:
            if cell.tag not in ("td", "th"):
                continue
            while (r, c) in grid:
                c += 1
            value = " ".join(" ".join(cell.itertext()).split()).replace("|", "\\|")
            rows = max(1, _span(cell.get("rowspan")))
            cols = max(1, _span(cell.get("colspan")))
            for dr in range(rows):  # 纵向合并往下带；横向合并只在第一格写值
                for dc in range(cols):
                    grid.setdefault((r + dr, c + dc), value if dc == 0 else SPAN)
            c += cols
    if not grid:
        return ""
    height, width = max(r for r, _ in grid) + 1, max(c for _, c in grid) + 1
    return markdown_table([[grid.get((r, c), "") for c in range(width)] for r in range(height)])


def _span(value) -> int:
    try:
        return min(int(value or 1), 1000)
    except ValueError:
        return 1


def strip_private_use(text: str) -> str:
    from clean.headings import strip_private_use as strip

    return strip(text)


def tidy(text: str) -> str:
    """解析结果的统一整理：HTML 表格转 Markdown 表格；<br> 记成换行；去掉 Word 项目符号留下的私用区字符。"""
    if not text:
        return text
    text = text.replace("\x00", "")  # 有的 PDF 文字层里有 NUL，PostgreSQL 的 text 存不了
    text = _HTML_TABLE.sub(lambda m: "\n\n" + html_table_to_markdown(m.group(0)) + "\n\n", text)
    # <br> 记成换行；管道表格的行里不换（格子里的几段用 <br> 隔开，换行会把一行表格拆断）
    text = "\n".join(line if line.lstrip().startswith("|") else re.sub(r"<br\s*/?>", "\n", line, flags=re.I)
                     for line in text.split("\n"))
    text = strip_private_use(text)
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# 完整性（A6）
COMPLETE_SHARE = 0.9
_MARKUP = re.compile(r"[\s|#\-]")


def incomplete(answer: dict, text: str, method: str, page_kind, copy) -> str | None:
    """解析结果明显不全时返回原因（"chars:实得/对照" 或 "pages:有内容页/总页"），否则 None。"""
    reference = answer.get("reference_chars")
    got = len(_MARKUP.sub("", text))
    if isinstance(reference, int) and reference > 0 and got < COMPLETE_SHARE * reference:
        return f"chars:{got}/{reference}"
    blocks = answer.get("content_list")
    if page_kind == "pdf_page" and method != "pypdf_text" and isinstance(blocks, list):
        total = _pdf_pages(copy)
        seen = {b.get("page_idx") for b in blocks if isinstance(b, dict) and b.get("type") not in SKIP_BLOCKS
                and _block_text(b).strip()}
        if total and len(seen) < COMPLETE_SHARE * total:
            return f"pages:{len(seen)}/{total}"
    return None


def _jats(path: Path, ext: str) -> dict:
    from clean import jats_text

    try:
        parsed = jats_text.parse(path)
    except (ValueError, OSError):
        return _result("failed", method="jats_xml", error="invalid_xml", ext=ext, attempts=["jats_xml:invalid_xml"])
    if parsed is None:
        return _result("unsupported", ext=ext, attempts=["jats_xml:not_jats"])
    if not parsed["text"].strip():
        return _result("failed", method="jats_xml", error="empty_text", ext=ext, attempts=["jats_xml:empty_text"])
    return _result("parsed", text=parsed["text"], method="jats_xml", ext=ext, attempts=["jats_xml:ok"],
                   segments=parsed["segments"], locator_kind=parsed["locator_kind"])


def parse_file(*, store_path: str, sha256: str, file_ext: str | None = None, **_ignored) -> dict:
    path = Path(store_path or "")
    unusable = _unusable_original(path, store_path, sha256, file_ext)
    if unusable is not None:
        return unusable
    ext = (file_ext or path.suffix or "").lower()
    if ext in JATS_EXTS:
        return _jats(path, ext)
    if ext not in SUPPORTED:
        with path.open("rb") as stream:
            head = stream.read(4 * 1024 * 1024)
        if is_web_page(head):  # 存成附件的其实是一个网页（旧采集把 CDE 的相关新闻页当成了附件）：写明，不当成未知文件
            return _result("unsupported", error="html_page_not_a_file", ext=".html")
        ext = _sniff(head)
    if ext not in SUPPORTED:
        return _result("unsupported", ext=ext)
    with tempfile.TemporaryDirectory(prefix="attachment-parse-") as work:
        # 解析器（尤其 LibreOffice）会在输入文件旁写东西：一律在临时副本上做；文件名带正确后缀
        copy = Path(work) / ("original" + ext)
        shutil.copyfile(path, copy)
        return without_text_layer(_parse_copy(copy, ext), path.stat().st_size)


def _unusable_original(path, store_path, sha256, file_ext):
    """The failure result for an original that is missing, too large or not the stored one; else None."""
    if not store_path or not path.is_file():
        return _result("failed", error="original_missing", ext=file_ext)
    if path.stat().st_size > max_parse_bytes():
        return _result("failed", error="too_large", ext=file_ext)
    if _sha256_file(path) != sha256:
        return _result("failed", error="original_digest_mismatch", ext=file_ext)
    return None


def _call_parser(call, copy):
    try:
        return call(str(copy))
    except Exception:  # noqa: BLE001 -- 解析服务/转换器故障记为失败，换下一个
        return {"success": False, "markdown": "", "error": "exception"}


def _parse_copy(copy, ext):
    """Each parser of the plan in turn on the working copy; the first one with text wins."""
    attempts, method, error, partial = [], None, "parser_failed", None
    plan = _plan(ext)
    for method, call, page_kind in plan:
        answer = _call_parser(call, copy)
        markdown = (answer.get("markdown") or "") if answer.get("success") else ""
        if answer.get("deferred"):  # 云端额度/限频：不是这个文件的失败，整份留到 retry_at 以后再做
            return {**_result("deferred", method=method, error=answer.get("error"), ext=ext,
                              attempts=[*attempts, method + ":" + str(answer.get("error"))]),
                    "retry_at": answer.get("retry_at")}
        if not markdown.strip():
            failed = "empty_text" if answer.get("success") else "parser_failed"
            detail = answer.get("error") if method in ("mineru_cloud", "pypdf_text") and not answer.get("success") \
                else ""
            attempts.append(method + ":" + failed + (":" + str(detail) if detail else ""))
            if method != "pypdf_text":  # 文字层不可用不算失败原因：结果按后面 MinerU 的失败原因记
                error = failed
            continue
        missing = ",".join(answer.get("missing_glyph_pages") or [])
        done = [method + ":ok"] + ([method + ":missing_glyphs:" + missing] if missing else [])
        result = _parsed(answer, markdown, method, page_kind, ext, [*attempts, *done])
        if missing:  # 有字读不出（没有字码映射）：正文照用，但如实写明哪几页缺字
            result["error"] = "missing_glyphs:" + missing
        short = incomplete(answer, result["text"], method, page_kind, copy)
        if short is None:
            return result
        attempts.append(method + ":incomplete:" + short)  # 不全：不当成功，换下一个解析器
        partial = partial or {**result, "status": "incomplete", "error": "incomplete:" + short}
    if partial is not None:
        return {**partial, "attempts": attempts}
    if ext == ".pdf" and attempts == ["pypdf_text:parser_failed:no_usable_text_layer"]:
        # 扫描件、又不调 MinerU：要 OCR
        return _result("unsupported", method="pypdf_text", error="text_layer_missing", ext=ext, attempts=attempts)
    return _result("failed", method=method, error=error, ext=ext, attempts=attempts)


def _parsed(answer, markdown, method, page_kind, ext, attempts):
    blocks = answer.get("content_list")
    if method == "excel":
        markdown = tidy(markdown)
        return _result("parsed", text=markdown, method=method, ext=ext, attempts=attempts,
                       segments=_sheets(markdown), locator_kind="sheet")
    if isinstance(blocks, list) and blocks:
        text, segments, kind = from_blocks(blocks, page_kind)
        if text.strip():
            return _result("parsed", text=text, method=method, ext=ext, attempts=attempts,
                           segments=segments, locator_kind=kind)
    return _result("parsed", text=tidy(markdown), method=method, ext=ext, attempts=attempts,
                   locator_kind="unavailable")
