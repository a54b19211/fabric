"""PDF 文字层 → 带表格的正文（pypdf 读字和画线的位置，不调外部服务）。NMPA 审计 A1。

为什么不直接用 pypdf.extract_text：表格的每一格是一小段字，extract_text 按出现顺序一格一行地吐出来
（四川局通告附件的表格变成“序\\n号\\n抽样\\n编号…”），读不出哪段字属于哪一格。

读法（每页）：
1. 用 pypdf 的 visitor 拿到每段字的位置（基线起点、字号），和画出来的线：细长的填充矩形、描边的矩形和直线
   （裁剪路径 `re W n` 不算）；
2. 横线、竖线连在一起、至少 2 横 2 竖、至少 2 列的一组线是一张表：横线的 y 和竖线的 x 切出格子；两格之间没有线
   就是合并格，合并格的字填进它占的每一格（每行单独看也完整）；格子里的字按行拼起来（中文之间不加空格）；
3. 表格以外的字按基线分行；一行写满到右边距、又没有以句末标点结尾的，和下一行接成一段（Word 自动换行）；
   页脚页码“- 2 -”去掉；
4. 对照：这样读出的字数比 extract_text 少 5% 以上时，这一页退回 extract_text（读法有漏，不丢字）。

第二轮审计（C-A4/A5）：
- 合并格：纵向合并的值往下带，横向合并只在第一格写值（跨过的格空着）；
- 接段：行尾是右括号、引号的不算句末（“（星光计划）”后面折行的“申报指南”接回去）；上一页最后一段没写完、下一页
  第一段不是标题或编号的，跨页接成一段（join_pages）；
- 字间空格：中文和“/”之间的空格去掉（“少发 /罕见”）；两段分开画的英文词，后一段大写开头的补空格（“forKids”）；
- 没有 Unicode 映射的字（中文字体子集用单字节编码、又没有 ToUnicode 表：红头、个别标题）：文字层里读出来是乱码
  或空白，这些字不收，页号记进 missing_glyph_pages（解析结果的 warnings），不编造。

第三轮审计（D1/D2）：
- 缺字：没有字码映射的字体画的字按指令里的字节数（不是可打印英文字符的字节），不按读出来的字数；
  页面上画的图片（高宽都不小于 IMAGE_MIN_SIDE 点）也算这一页有读不出的字：四川局通告、星光计划的红头、标题、
  子附件的“附件 1”是贴在页面上的图片，文字层里只有空格，以前没有标记；
- 接段：行尾“：”但括号还没闭合的（“技术问题（包括但不限于：”）不算段落结束；“附件：1.……（星光计划）”折下来对齐在
  编号后面的“申报指南”接回去；下一行以项目符号开头的不接；“第三章）和/或……”不是编号开头；
- 中文和“/”“-”之间不加空格（“国家/地区”“局限性/不确定性”“暴露-效应”）；Wingdings 的 ➢ 照原样（headings.symbol）。
"""

from __future__ import annotations

import bisect
import math
import re

from clean.excel_parser import SPAN, markdown_table

_END_PUNCT = "。：:；;！!？?"
_LIST_START = re.compile(r"^(?:[一二三四五六七八九十]+[、．.]|[（(][一二三四五六七八九十\d]+[)）]|\d+[、．.]|"
                         r"第[一二三四五六七八九十\d]+[章节条](?![）)，,、。；;]))")
_LISTING = re.compile(r"附\s*[件表]?\s*[：:]")  # “附件：1.……”清单，折下来的行对齐在编号后面
_NUMBER_GAP = re.compile(r"^\S{1,5}? {2,}")  # 行首编号或项目符号后面的缩进
_BULLETS = "•◆-" + "".join(map(chr, (0xF0B7, 0xF06C, 0xF0A7, 0xF0D8, 0xF076)))  # 项目符号（含 Word 符号字体的私用区字符）
_PAGE_NUMBER = re.compile(r"^[-—–]\s*\d+\s*[-—–]$")
_CJK_FONT = re.compile(r"GB2312|GBK|SimSun|SimHei|NSimSun|FangSong|KaiTi|Kai|Song|Hei|YaHei|STXihei|DengXian|"
                       r"[\u4e00-\u9fff]", re.I)


def _nonspace(text: str) -> int:
    return len("".join(text.split()))


def join_cjk(a: str, b: str) -> str:
    """两段字接起来：两边都是非 ASCII（中文）时不加空格，否则加一个空格。"""
    if not a:
        return b
    if not b:
        return a
    if ord(a[-1]) > 127 and ord(b[0]) > 127:
        return a + b
    if ord(a[-1]) > 127 or ord(b[0]) > 127:
        return a + b if a[-1] in "（(《“/-" or b[0] in "，。、；：）)》”/-" else a + " " + b
    return a + " " + b


def _width(text: str, size: float) -> float:
    return sum(size if ord(c) > 127 else size * 0.5 for c in text)


class _Page:
    def __init__(self, page):
        self.frags, self.h, self.v, self.missing = [], [], [], 0
        self._path, self._cur, self._start = [], None, None
        self._fonts, self._unmapped_font = _page_fonts(page), False
        self._images = _page_images(page)
        self._actual, self._marks = actual_texts(page), []  # 结构树里的 ActualText；正在画的标记内容（MCID）栈
        self._lost = {}  # MCID（None：不在标记内容里）→ [读不出的字数, 字码数, 图片数, 第一次画的位置]
        page.extract_text(visitor_operand_before=self._op, visitor_text=self._text)
        self._recover()

    def _mcid(self):
        return next((m for m in reversed(self._marks) if m is not None), None)

    def _note(self, slot, amount, where=None):
        entry = self._lost.setdefault(self._mcid(), [0, 0, 0, None])
        entry[slot] += amount
        if where and entry[3] is None:
            entry[3] = where

    def _recover(self):
        """读不出的字（没有字码映射的字体、贴图、只画了空格）：所在的标记内容在结构树里有 ActualText 的，用它补回
        （Word 导出的 PDF 把红头、标题的字写在这里，第四轮 D-A）；补不回的记缺字。"""
        missing = 0
        for mcid, (chars, codes, images, where) in self._lost.items():
            actual = self._actual.get(mcid) if mcid is not None else None
            blank = bool(where) and where[3]  # 这段标记内容画的是图片、读不出的字或只有空格
            if actual and blank:
                self.frags = [f for f in self.frags if f.get("mcid") != mcid]
                x, y, w, size = where[0], where[1], where[2], where[4]
                if w:  # 图片：字号按宽度均分，不超过图片高度
                    size = min(size, w / max(1.0, _width(actual, 1.0)))
                self.frags.append({"x": x, "y": y, "size": size, "text": actual, "mcid": mcid})
            else:
                missing += max(chars, codes) + images
        self.missing = missing

    # -- 字 --
    def _text(self, text, cm, tm, font, size):
        if not text:
            return
        scale = math.hypot(tm[0], tm[1]) * math.hypot(cm[0], cm[1]) or 1.0
        eff = (size or 10) * scale
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        mcid, unmapped = self._mcid(), _unmapped(font)
        if mcid is not None and self._actual.get(mcid) and (tm[4] or tm[5]):
            # 有 ActualText 的标记内容第一次画字的位置；只画了空格或读不出的字的，算“没读出”（红头“通 告”是空格加图形）
            self._note(0, 0, (x, y, 0, not text.strip() or unmapped, eff))
            entry = self._lost[mcid]
            if text.strip() and not unmapped and entry[3][3]:
                entry[3] = entry[3][:3] + (False,) + entry[3][4:]
        if unmapped:  # 中文字体子集没有字码映射：汉字读出来是乱码，不收（记下缺字）；数字、英文照收
            kept = _ascii_only(text)
            self._note(0, len("".join(text.split())) - len("".join(kept.split())))
            text = kept
            if not text.strip():
                return
        last = self.frags[-1] if self.frags else None
        if tm[4] == 0 and tm[5] == 0 and last:  # pypdf 对同一行接着显示的字给 (0,0)：接在上一段后面
            last["text"] += text
            return
        if not text.strip():
            return
        self.frags.append({"x": x, "y": y, "size": eff, "text": text, "mcid": mcid})

    # -- 线 --
    def _op(self, op, args, cm, tm):
        def pt(x, y):
            return (cm[0] * x + cm[2] * y + cm[4], cm[1] * x + cm[3] * y + cm[5])

        try:
            if self._marked(op, args, cm):
                return
            if op == b"re":
                x, y, w, h = (float(a) for a in args[:4])
                self._path.append(("rect", pt(x, y), pt(x + w, y + h)))
            elif op == b"m":
                self._cur = self._start = pt(float(args[0]), float(args[1]))
            elif op == b"l" and self._cur is not None:
                nxt = pt(float(args[0]), float(args[1]))
                self._path.append(("line", self._cur, nxt))
                self._cur = nxt
            elif op == b"h" and self._cur is not None and self._start is not None:
                self._path.append(("line", self._cur, self._start))
            elif op in (b"S", b"s", b"B", b"B*", b"b", b"b*"):
                self._commit(stroke=True)
            elif op in (b"f", b"F", b"f*"):
                self._commit(stroke=False)
            elif op == b"n":
                self._path, self._cur = [], None
        except (TypeError, ValueError, IndexError):
            self._path, self._cur = [], None

    def _marked(self, op, args, cm) -> bool:
        """字体、标记内容（BDC/BMC/EMC）、读不出的字和图片：处理了返回 True。"""
        if op == b"Tf":
            self._unmapped_font = _unmapped(self._fonts.get(str(args[0])))
        elif op == b"BDC":
            props = args[1] if len(args) > 1 else None
            self._marks.append(props.get("/MCID") if isinstance(props, dict) else None)
        elif op == b"BMC":
            self._marks.append(None)
        elif op == b"EMC":
            if self._marks:
                self._marks.pop()
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            if self._unmapped_font:
                self._note(1, _drawn_codes(args))
        elif op == b"Do" and str(args[0]) in self._images:
            w, h = abs(cm[0]) + abs(cm[2]), abs(cm[1]) + abs(cm[3])
            if min(w, h) >= IMAGE_MIN_SIDE:  # 贴在页面上的图片（红头、图片标题、印章）：里面的字文字层里没有
                self._note(2, 1, (cm[4], cm[5] + 0.15 * h, w, True, h))
        else:
            return False
        return True

    def _commit(self, stroke):
        for kind, (x0, y0), (x1, y1) in self._path:
            x0, x1 = sorted((x0, x1))
            y0, y1 = sorted((y0, y1))
            w, h = x1 - x0, y1 - y0
            if kind == "rect":
                if h <= 3 and w > 3:
                    self.h.append(((y0 + y1) / 2, x0, x1))
                elif w <= 3 and h > 3:
                    self.v.append(((x0 + x1) / 2, y0, y1))
                elif stroke and w > 3 and h > 3:  # 描边的矩形：四条边都是线；填充的大矩形是底色，不算
                    self.h += [(y0, x0, x1), (y1, x0, x1)]
                    self.v += [(x0, y0, y1), (x1, y0, y1)]
            elif stroke:
                if h < 1 and w > 3:
                    self.h.append((y0, x0, x1))
                elif w < 1 and h > 3:
                    self.v.append((x0, y0, y1))
        self._path, self._cur = [], None


def actual_texts(page) -> dict:
    """这一页标记内容（MCID）→ 结构树里写的 ActualText（标注过的 PDF 才有；读不出就空）。整份文件读一次，记在 reader 上。"""
    try:
        reader, ref = page.pdf, page.indirect_reference
        cache = getattr(reader, "_actual_texts", None)
        if cache is None:
            cache = {}
            root = reader.trailer["/Root"].get_object().get("/StructTreeRoot")
            if root is not None:
                _walk_struct(root.get_object(), None, None, cache)
            reader._actual_texts = cache
        return cache.get(ref.idnum, {}) if ref is not None else {}
    except Exception:  # noqa: BLE001 -- 结构树读不出：不补
        return {}


def _walk_struct(node, page, actual, out, depth=0):
    """结构树往下走：带 ActualText 的元素，这段字记在它（含子元素）的第一个 MCID 上。"""
    if depth > 100:
        return
    node = node.get_object() if hasattr(node, "get_object") else node
    if isinstance(node, int):
        if page is not None and actual and not actual[1]:
            out.setdefault(page, {}).setdefault(node, actual[0])
            actual[1] = True
        return
    if isinstance(node, list):
        for item in node:
            _walk_struct(item, page, actual, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    pg = node.get("/Pg")
    page = pg.idnum if hasattr(pg, "idnum") else page
    if "/MCID" in node:  # 标记内容引用 {/Type /MCR /MCID n}
        _walk_struct(int(node["/MCID"]), page, actual, out, depth + 1)
        return
    if actual is None and "/ActualText" in node and str(node["/ActualText"]).strip():
        actual = [" ".join(str(node["/ActualText"]).split()), False]
    kids = node.get("/K")
    if kids is not None:
        _walk_struct(kids, page, actual, out, depth + 1)


def _page_fonts(page) -> dict:
    """这一页的字体字典 {资源名: 字体}（读不出就空）。"""
    try:
        fonts = (page.get("/Resources") or {}).get_object().get("/Font") or {}
        return {str(k): v.get_object() for k, v in fonts.get_object().items()}
    except Exception:  # noqa: BLE001 -- 资源读不出：不按字体判缺字
        return {}


IMAGE_MIN_SIDE = 10  # 点；更细的是装饰线


def _page_images(page) -> set:
    """这一页资源里的图片 XObject 名字（读不出就空）。"""
    try:
        objects = (page.get("/Resources") or {}).get_object().get("/XObject") or {}
        return {str(k) for k, v in objects.get_object().items() if v.get_object().get("/Subtype") == "/Image"}
    except Exception:  # noqa: BLE001
        return set()


def _drawn_codes(args) -> int:
    """Tj/TJ 指令里画的字节中不是可打印英文字符的个数（没有字码映射的中文字体画的汉字；数字、英文、标点照收）。"""
    count = 0
    for arg in args:
        items = arg if isinstance(arg, list) else [arg]
        for item in items:
            raw = getattr(item, "original_bytes", item)
            if isinstance(raw, (bytes, bytearray)):
                count += sum(1 for b in raw if not 0x20 <= b <= 0x7E)
    return count


def _ascii_only(text: str) -> str:
    return "".join(c if ord(c) < 128 else " " for c in text)


def _unmapped(font) -> bool:
    """中文字体子集按单字节编码（WinAnsi 等）又没有 ToUnicode 表：字形读不出是哪个字。"""
    try:
        if not font or "/ToUnicode" in font or font.get("/Subtype") == "/Type0":
            return False
        return bool(_CJK_FONT.search(str(font.get("/BaseFont") or "")))
    except Exception:  # noqa: BLE001 -- 字体字典读不出：照常收字
        return False


def _merge(lines, tol=1.5, gap=2.0):
    """同一位置（±tol）上首尾相接的线段并成一条：[(pos, a0, a1)]。"""
    out = []
    for pos, a0, a1 in sorted(lines):
        for item in out:
            if abs(item[0] - pos) <= tol and a0 <= item[2] + gap and a1 >= item[1] - gap:
                item[1], item[2] = min(item[1], a0), max(item[2], a1)
                break
        else:
            out.append([pos, a0, a1])
    return out


def _cluster(values, tol=2.0):
    out = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _tables(h, v):
    """一组组相交的横竖线 → [(xs 递增, ys 递减, 横线, 竖线)]。"""
    h, v = _merge(h), _merge(v)
    tol = 2.5
    parent = list(range(len(h) + len(v)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (y, x0, x1) in enumerate(h):
        for j, (x, y0, y1) in enumerate(v):
            if x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol:
                parent[find(i)] = find(len(h) + j)
    groups = {}
    for i in range(len(h) + len(v)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in groups.values():
        hs = [h[i] for i in members if i < len(h)]
        vs = [v[i - len(h)] for i in members if i >= len(h)]
        xs, ys = _cluster([x for x, _, _ in vs]), _cluster([y for y, _, _ in hs])[::-1]
        if len(xs) >= 3 and len(ys) >= 2:
            out.append((xs, ys, hs, vs))
    return out


def _lines(frags):
    """字段按基线分行（容差取字号的一半），行内按 x 排；返回 [(y, x0, x1, text, size)]，从上到下。"""
    rows = []
    for f in sorted(frags, key=lambda f: (-f["y"], f["x"])):
        if rows and abs(rows[-1][0][0]["y"] - f["y"]) <= 0.5 * max(f["size"], rows[-1][0][0]["size"]):
            rows[-1][0].append(f)
        else:
            rows.append(([f],))
    out = []
    for (items,) in rows:
        items.sort(key=lambda f: f["x"])
        text, end = "", None
        main = max(items, key=lambda f: (f["size"], len(f["text"])))
        for f in items:
            piece = f["text"].replace("\n", "")  # pypdf 把后面画的空格、换行接在同一段里（“——  \n\n”）
            place = _script_place(f, main, end)
            if place:  # 字号小、基线偏下（上）、紧贴前一个字的短字：下标、上标（“Cmax”的 max，第四轮 D-B）
                from clean.headings import scripted
                text += scripted(piece, place)
            else:
                text = _append(text, piece, f, end)
            end = f["x"] + _width(piece, f["size"])
        text = re.sub(r" {3,}", "  ", text.strip())
        text = re.sub(r"(?<![ \x00-\x7f]) (?![ \x00-\x7f])", "", text)  # 两端对齐撑出来的中文字间空格
        text = re.sub(r"(?<=[^\x00-\x7f]) (?=[/-])|(?<=[/-]) (?=[^\x00-\x7f])", "", text)  # “少发 /罕见”“暴露 -效应”
        if text:
            out.append((items[0]["y"], items[0]["x"], end, text, max(f["size"] for f in items)))
    return out


_CJK_SPACE = re.compile(r"[^\x00-\x7f] +$")


def _append(text, piece, f, end) -> str:
    """一行里接上一段字。"""
    if end is not None and f["x"] - end > 1.5 * f["size"]:
        return text.rstrip() + "  " + piece.lstrip()  # 同一行里隔得远（落款、表外的两栏）：留两个空格
    if end is not None and text[-1:].isalnum() and text[-1:].isascii() and piece[:1].isalnum() \
            and piece[:1].isascii() and (f["x"] - end > 0.2 * f["size"] or (text[-1:].islower() and piece[:1].isupper())):
        return text + " " + piece  # 英文词之间的空格在 PDF 里常是位置间隔，不是空格字符（“for”“Kids”分开画）
    # 中文后面画的空格紧接着又是中文（“——  ”“星光计划”）：空格不要
    return (text.rstrip() if _CJK_SPACE.search(text) and ord(piece[:1] or " ") > 127 else text) + piece


def _script_place(f, main, end):
    """这段字是不是紧跟在前一个字后面的下标（"sub"）、上标（"sup"）：字号不到这一行主字号的 3/4、不超过 6 个字、
    离前一个字不到半个字宽、基线比主字低（高）一成字号以上。"""
    piece = f["text"].strip()
    if end is None or f is main or not piece or len(piece) > 6 or f["size"] > 0.75 * main["size"] \
            or abs(f["x"] - end) > 0.5 * main["size"]:
        return None
    shift = f["y"] - main["y"]
    if shift <= -0.05 * main["size"]:
        return "sub"
    if shift >= 0.2 * main["size"]:
        return "sup"
    return None


def _cell_text(frags, right) -> str:
    """一格的字：一行一行接起来；上一行写满到格子右边（自动换行把一串编号、地址拆开）时，英文数字直接接上不加空格。"""
    text, full = "", False
    for _, _, x1, line, size in _lines(frags):
        line = " ".join(line.split())
        if full and text[-1:].isascii() and text[-1:] != " " and line[:1].isascii():
            text += line
        else:
            text = join_cjk(text, line)
        full = x1 >= right - 1.5 * size
    return text.replace("|", "\\|")


def _table_markdown(table, frags):
    xs, ys, hs, vs = table
    rows, cols = len(ys) - 1, len(xs) - 1
    tol = 2.5

    def vline_at(xi, y):
        return any(abs(x - xs[xi]) <= tol and y0 - tol <= y <= y1 + tol for x, y0, y1 in vs)

    def hline_at(yi, x):
        return any(abs(y - ys[yi]) <= tol and x0 - tol <= x <= x1 + tol for y, x0, x1 in hs)

    parent = {(r, c): (r, c) for r in range(rows) for c in range(cols)}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for r in range(rows):
        ymid = (ys[r] + ys[r + 1]) / 2
        for c in range(cols):
            xmid = (xs[c] + xs[c + 1]) / 2
            if c + 1 < cols and not vline_at(c + 1, ymid):
                parent[find((r, c))] = find((r, c + 1))
            if r + 1 < rows and not hline_at(r + 1, xmid):
                parent[find((r, c))] = find((r + 1, c))
    members = {}
    for f in frags:
        c = bisect.bisect_right(xs, f["x"] + 0.5) - 1
        r = bisect.bisect_right([-y for y in ys], -f["y"]) - 1
        r, c = min(max(r, 0), rows - 1), min(max(c, 0), cols - 1)  # 压在边线上的字算进最近的格，不丢
        members.setdefault(find((r, c)), []).append(f)
    rights = {}
    for (r, c) in parent:
        k = find((r, c))
        rights[k] = max(rights.get(k, 0), xs[c + 1])
    texts = {k: _cell_text(v, rights[k]) for k, v in members.items()}
    return markdown_table(_span_grid(rows, cols, find, texts))


def _span_grid(rows, cols, find, texts):
    """合并格：每行里同一个合并格只在最左一格写值，右边跨过的格空着（SPAN）；上下合并的每行都写（往下带）。"""
    def cell(r, c):
        return SPAN if c > 0 and find((r, c - 1)) == find((r, c)) else texts.get(find((r, c)), "")
    return [[cell(r, c) for c in range(cols)] for r in range(rows)]


def _inside(f, table):
    xs, ys = table[0], table[1]
    return xs[0] - 1 <= f["x"] <= xs[-1] + 1 and ys[-1] - 1 <= f["y"] <= ys[0] + 1


def _paragraphs(lines) -> list[str]:
    """表格以外的行接成段：上一行写到右边距附近、不以句末标点结尾，这一行从左边距起（新段落首行缩进两字）、
    不是编号开头的，接到上一行后面。"""
    if not lines:
        return []
    # 右边距：长行（15 字以上）行尾位置的 3/4 分位，不用最右的一行（目录、表格外的长行会把边距撑宽，正常写满的行
    # 就不算“写满”，一句话被拆成两段：“申请加入星”“光计划的具体方案”）
    ends = sorted(x1 for _, _, x1, text, _ in lines if len(text) >= 15) or [max(x1 for _, _, x1, _, _ in lines)]
    right = ends[(3 * (len(ends) - 1)) // 4]
    left = min(x0 for _, x0, _, _, _ in lines)
    out, current, joinable, nearly, first = [], "", False, False, left
    for _, x0, x1, text, size in lines:
        limit = _wrap_limit(current, first, left, size)
        tail = nearly and len(text) <= 4  # 折下来只剩一两个字（“……未满足的临床需”“求”），上一行差一点写满
        if current and (joinable or tail or _reference_wrap(current, text)) and x0 <= limit \
                and not _LIST_START.match(text) and text[:1] not in _BULLETS:
            current = join_cjk(current, text)
        else:
            if current:
                out.append(current)
            current, first = text, x0
        joinable, nearly = _open_line(text, x1, right, size)
    if current:
        out.append(current)
    return out


def _wrap_limit(current, first, left, size):
    """折下来的行最多从哪里起：编号、项目符号开头的段落是悬挂缩进，折下来的行对齐编号后面的字（比这一段第一行
    靠右一两个字）；“附件：1.……”对齐在“1.”后面的字；其余从左边距起。"""
    if _LISTING.match(current):
        return first + 6 * size
    if _LIST_START.match(current) or current[:1] in _BULLETS:
        return first + 2.5 * size
    return left + 0.5 * size


_REFERENCE = re.compile(r"\d{1,3}[.．]\s*[A-Za-z]")


def _reference_wrap(current, text) -> bool:
    """英文参考文献条目（“2.US FDA Guidance for Industry: Pediatric Study Plans:”）折下来的行：条目还没以“.”结束、
    下一行是英文或数字开头，就接上（英文行的宽度估不准，不靠“写满”判断，第四轮 D-B）。"""
    return bool(_REFERENCE.match(current)) and current[-1] != "." and (current[-1].isascii() or current[-1] in "–—") \
        and text[:1].isascii() and text[:1].isalnum()


def _open_line(text, x1, right, size):
    """(这一行写满了、句子没完：下一行接上；差一点写满：下一行只剩一两个字也接上)。行里隔得远的两段（落款、两栏）
    不接；行首编号、项目符号后面的缩进（“1.  本实施框架”“•  拟开发”）不算隔得远。"""
    unclosed = text.count("（") + text.count("(") > text.count("）") + text.count(")")
    open_end = (text[-1] not in _END_PUNCT and text[-1] != ".") or (text[-1] in "：:" and unclosed)
    joinable = x1 >= right - 4 * size and open_end and "  " not in _NUMBER_GAP.sub("", text)
    return joinable, x1 >= right - 8 * size and len(text) >= 15 and open_end and "  " not in text


def _is_page_number(text: str) -> bool:
    return bool(_PAGE_NUMBER.match(text) or re.fullmatch(r"\d{1,3}|第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?", text))


def page_text(page) -> str:
    """一页的正文：段落一行一段（段间空行），表格是 Markdown 表格，按在页面上的上下顺序。"""
    return page_reading(page)[0]


def page_reading(page) -> tuple[str, int]:
    """(一页的正文, 读不出的字数：没有字码映射的字体画的字)。"""
    plain = page.extract_text() or ""
    try:
        reader = _Page(page)
    except Exception:  # noqa: BLE001 -- 位置读不出：用 extract_text
        return plain, 0
    if reader.missing:
        plain = _without_unmapped(page, plain)
    tables = _tables(reader.h, reader.v)
    flow = [f for f in reader.frags if not any(_inside(f, t) for t in tables)]
    lines = _without_page_numbers(_lines(flow), tables)
    cuts = sorted(((t[1][0], _table_markdown(t, [f for f in reader.frags if _inside(f, t)])) for t in tables),
                  key=lambda x: -x[0])
    text = "\n\n".join(i for i in _interleave(lines, cuts) if i)
    if _nonspace(text.replace("|", "").replace("---", "")) < 0.95 * _nonspace(plain) - 5:
        return plain, reader.missing
    return text, reader.missing


def _without_unmapped(page, plain):
    """extract_text 的对照文字里也去掉读不出的字体画的字（不然对照偏多、退回到带乱码的 extract_text）。"""
    kept = []

    def visit(text, cm, tm, font, size):
        if text:
            kept.append(_ascii_only(text) if _unmapped(font) else text)
    try:
        page.extract_text(visitor_text=visit)
    except Exception:  # noqa: BLE001
        return plain
    return "".join(kept)


def _last_paragraph_open(text: str) -> bool:
    """一页最后一段没写完：不以句末标点结尾，或括号还没有闭合（“（包括但不限于：”）。"""
    last = text.rsplit("\n\n", 1)[-1].strip()
    if len(last) < 15 or last.startswith("|") or "\n" in last:
        return False
    return last[-1] not in _END_PUNCT + "." or last.count("（") > last.count("）")


def join_pages(texts: list[str]) -> list[str]:
    """上一页最后一段没写完（不以句末标点结尾），下一页第一段不是表格、标题、编号开头的：把下一页的第一段
    接到上一页最后一段后面（跨页断开的词接回去：“……儿”“童人群剂型开发”）。"""
    from clean.headings import rank

    out = list(texts)
    for i in range(1, len(out)):
        head, _, rest = out[i].strip().partition("\n\n")
        if out[i - 1] and head and _table_cell_continues(out[i - 1], head):
            out[i - 1] = _continue_last_cell(out[i - 1].rstrip(), head.strip())
            out[i] = rest
            continue
        if not out[i - 1] or not head or not _last_paragraph_open(out[i - 1]):
            continue
        if head.startswith("|") or _LIST_START.match(head) or rank(head) or _is_page_number(head):
            continue
        out[i - 1] = join_cjk(out[i - 1].rstrip(), head.strip())
        out[i] = rest
    return out


def _table_cell_continues(previous: str, head: str) -> bool:
    """上一页以表格结束、最后一格的字没写完（不以句末标点结尾），这一页第一段不是表格、标题、编号、页码：
    （最后一格至少 15 字）它是最后一格跨页的续行（“……现有数据”“如何支持儿童研究和使用。……”，第四轮 D-B）。"""
    from clean.headings import rank

    last = previous.rstrip().rsplit("\n", 1)[-1]
    if not last.startswith("|") or "\n" in head or head.startswith("|"):
        return False
    cells = [c.strip() for c in last.strip().strip("|").split(" | ")]
    filled = [c for c in cells if c]
    if not filled or filled[-1] != cells[-1] or len(filled[-1]) < 15 or filled[-1][-1] in _END_PUNCT + "）)":
        return False
    return not (_LIST_START.match(head) or rank(head) or _is_page_number(head) or head.startswith("（"))


def _continue_last_cell(previous: str, head: str) -> str:
    body, _, last = previous.rpartition("\n")
    inner = last.rstrip()[:-1].rstrip()  # 去掉行尾的“|”
    return (body + "\n" if body else "") + join_cjk(inner, head.replace("|", "\\|")) + " |"


def _without_page_numbers(lines, tables):
    """页眉页脚的页码（“- 2 -”、单独一个数字）：只看整页最下（或最上）的那一行，表格也算在内。"""
    lowest = min([t[1][-1] for t in tables], default=math.inf)
    highest = max([t[1][0] for t in tables], default=-math.inf)
    if lines and _is_page_number(lines[-1][3]) and lines[-1][0] < lowest:
        lines = lines[:-1]
    if lines and _is_page_number(lines[0][3]) and lines[0][0] > highest:
        lines = lines[1:]
    return lines


def _interleave(lines, cuts):
    """表格把正文分成上下几块：每块各自接段，再按 y 和表格（cuts：[(表格顶边 y, Markdown)]，从上到下）穿插。"""
    items, chunk, ci = [], [], 0
    for line in lines:
        while ci < len(cuts) and line[0] < cuts[ci][0]:
            items += _paragraphs(chunk)
            chunk = []
            items.append(cuts[ci][1])
            ci += 1
        chunk.append(line)
    return items + _paragraphs(chunk) + [md for _, md in cuts[ci:]]
