"""Excel 附件 → Markdown 表格（.xlsx 用 openpyxl 读缓存的公式值，.xls 用 xlrd）。

规则（NMPA 审计 A2/A3/A10）：
- 只取实际有字的范围：不信文件里声明的范围（有的 xlsx 声明 A1:XFD1048576，以前补成 16,384 列、29.5 万字）；
  空行去掉，首尾全空的列去掉；
- 合并单元格（第二轮审计 A7/D3/D10）：纵向合并的值往下带（每行单独看也完整）；横向合并的值只写在第一格，
  跨过的格空着（以前“序号 | 序号”、一串勾选项重复 9 次）。读的时候跨过的格先记成 SPAN，两层表头合并时当作左边
  那格，写出时是空格子；
- 表头：第一行明显是表头（调用方说是，或格子大多是“名称”“序号”“日期”这类栏目名）才当表头；不像表头的
  （“| 心脏疾病 | 心肌缺血 | 未知 |”）表头行空着，第一行照样是数据；
- 表名上方的纵向合并（表名占两行）只写一次；只有一个 Sheet 的工作簿不写“## Sheet1”；
- 表格不补空格：`| a | b |`，分隔行 `| --- | --- |`；
- 单元格里的换行写成 `<br>`（和 Word 表格同一写法，第三轮审计 N3），`|` 转义成 `\\|`，一行表格永远是一行；
- 表格上方合并成一格的表名行（“2017年……统计表”：值所在的格本身横跨几列）写成表格前的一行字，不当表头；
  “申请编号 | （空着待填）”这种标签行、格子里分了几行的表单行留在表里（第三轮审计 D8）；
- 没有表头行的表格（第三轮审计 D5）：前几行里第一个格子都有字、像栏目名的行（“序号 | 产品名称 | …”）当表头，它上面的
  行照原顺序放在表头下面；没有这样的行就用“列1 | 列2 | …”。数据行不当表头；长表格分组时每组都带这个表头；
- 每个 Sheet 一个 `## 名称` 小标题，空 Sheet 跳过。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from loguru import logger


def cell_text(value) -> str:
    """单元格值 → 一行字（换行变空格，| 转义）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        value = int(value) if value.is_integer() else value
    elif isinstance(value, dt.datetime):
        value = value.date().isoformat() if value.time() == dt.time() else value.isoformat(sep=" ")
    elif isinstance(value, (dt.date, dt.time)):
        value = value.isoformat()
    lines = (" ".join(line.split()) for line in str(value).splitlines())
    return "<br>".join(line for line in lines if line).replace("|", "\\|")


SPAN = "\x00span"  # 横向合并跨过的格（读表时的记号，写出时是空格子）
# 栏目名常见的结尾字：表头行的格子大多以这些字结尾（“产品名称”“序号”“生产日期”“检验结果”）
_HEADER_ENDINGS = set("号称期名人位量型格别注果据构间址况见由款节类式求准系目容品业次话箱级源标数率值限码种例"
                      "容况员门商地点域途法项件证书限价额径度态性因施理议核查论述明识")


def _real(cell) -> str:
    return "" if cell == SPAN else cell


def _named(row) -> tuple[int, list[str]]:
    """(以栏目名常见的字结尾的格子数, 有字的格子)；“产品类别（一级）”按“产品类别”看。"""
    cells = [re.sub(r"[（(][^）)]{1,20}[)）]$", "", c.replace("\\|", "").strip()) or c
             for c in row if _real(c).strip()]
    return sum(1 for c in cells if len(c) <= 20 and c[-1] in _HEADER_ENDINGS), cells


def is_header_row(row) -> bool:
    """第一行像不像表头：有字的格子里至少 3/4 以栏目名常见的字结尾；或一半以上是、而且格子都是不带数字的短词
    （“情形 | 市场独占期 | 中药 | 化药 | 备注”）。两列的表（“企业名称 | 甲公司”这种字段名 | 值）要全是栏目名。"""
    named, cells = _named(row)
    if not cells:
        return False
    if len(row) <= 2:
        return named == len(cells)
    short = all(len(c) <= 12 and not re.search(r"\d", c) for c in cells)
    return named >= 0.75 * len(cells) or (short and named >= 0.5 * len(cells))


def _trim(rows: list[list[str]]) -> list[list[str]]:
    """去掉全空行和首尾全空的列，各行补到同样的列数。"""
    rows = [r for r in rows if any(_real(c) for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    used = [i for i in range(width) if any(_real(r[i]) for r in rows)]
    first, last = used[0], used[-1]
    return [r[first:last + 1] for r in rows]


def _title_row(row, titles) -> bool:
    """表名行：只有一个值，格里没有分行。titles="merged"（Word、PDF、网页表格）：值所在的格还要横跨几列（右边是
    SPAN）——“申请编号 | （空格子待填）”是表单的标签行，不是表名；"single"（Excel：表名常写在 A1、不合并）：一个值就算。"""
    values = [i for i, c in enumerate(row) if _real(c)]
    if len({row[i] for i in values}) != 1 or "<br>" in row[values[0]]:
        return False
    if titles == "single":
        return True
    first = values[0]
    return first + 1 < len(row) and row[first + 1] == SPAN and all(c == SPAN or not c for c in row[first + 1:])


def _lead_rows(rows, titles="merged"):
    """表格上方的表名行（_title_row）：取出来写成表前的字；同一个表名纵向合并占两行的只写一次。"""
    lead = []
    while len(rows) > 1 and len(rows[0]) > 1 and _title_row(rows[0], titles):
        value = next(c for c in rows[0] if _real(c))
        if not lead or lead[-1] != value:
            lead.append(value)
        rows = _trim(rows[1:])
    return lead, rows


def markdown_table(rows: list[list[str]], header: bool | None = None, titles: str = "merged") -> str:
    """二维字符串 → Markdown 表格；表格上方只有一个值的行写成单独一行字。不补空格。
    header：第一行是不是表头（None：按 is_header_row 看）；不是表头时表头行空着。"""
    lead, rows = _lead_rows(_trim(rows), titles)
    if not rows:
        return "\n\n".join(lead)
    cut = _next_subtable(rows)
    if cut:  # Word 里连成一张的两张分表：后一张从它自己的分组行、栏目名行起另起一张（复查6 D2）
        return "\n\n".join(lead + [markdown_table(rows[:cut], header, titles), markdown_table(rows[cut:], None, titles)])
    rows = _merge_header_rows(rows)
    if len(rows[0]) == 1:  # 只有一列：不是表格，一行一段
        return "\n\n".join(lead + [_real(r[0]) for r in rows])
    width = len(rows[0])
    above, rows = _with_header(rows, header)
    lead += [_row_line(r) for r in above if not _group_row(r)]
    rows = [[_real(c) for c in r] for r in rows]
    rows[0] = _with_groups(rows[0], [r for r in above if _group_row(r)])
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    # 整行合并成一格的行（表中间的小标题“总体意见建议”）：值只写在第一格，不重复整行
    lines += ["| " + " | ".join(r if len(set(r)) > 1 else [r[0]] + [""] * (width - 1)) + " |" for r in rows[1:]]
    return "\n\n".join(lead + ["\n".join(lines)])


def _full(row) -> bool:
    """格子都有字（SPAN 算有字），四列以上的允许空一格（表头里空着的角）；至少两个格子有字（合并成一整格的
    “单位/企业名称<br>填写人”是表单的一栏，不是栏目名）。"""
    empty = sum(1 for c in row if c != SPAN and not c.strip())
    values = sum(1 for c in row if _real(c).strip())
    return values >= min(2, len(row)) and (empty == 0 or (len(row) >= 4 and empty == 1))


def _with_header(rows, declared):
    """(表头上面的行, 表格行)：第一行是表头（调用方说是，或像栏目名）就是它；否则前几行里第一个格子都有字、像栏目名
    的行当表头，它上面的行（表单的填写行“单位/企业名称 填写人”、分组行“第一类医疗器械产品目录 | 新医疗器械分类目录”）
    照原顺序写在表格前面（第四轮 D-D：不挪到表头下面）；都没有就拿第一行当表头，和同类表格一样，不补原文没有的
    “列1 | 列2”（第四轮 N2）。"""
    first = _full(rows[0]) and (is_header_row(rows[0]) if declared is None else declared)
    grouped = sum(1 for c in rows[0] if c == SPAN or not c.strip()) * 2 >= len(rows[0])  # 上层分组行（大半是合并格）
    if first and not grouped:
        return [], rows
    for i in range(1, min(HEADER_SEARCH_ROWS, len(rows))):
        if _label_row(rows[i]):
            return rows[:i], rows[i:]
    return [], rows


def _row_line(row) -> str:
    """表头上面的一行写成表前的一行字：有字的格子去重后用“ | ”隔开，格内分行用空格。"""
    values = dict.fromkeys(_real(c).replace("<br>", " ") for c in row if _real(c).strip())
    return " | ".join(values)


HEADER_SEARCH_ROWS = 4


def _group_row(row) -> bool:
    """表头上面的分组行（“第一类医疗器械产品目录 | 新医疗器械分类目录”“6801 基础外科手术器械 | 对应关系 | 备注”）：
    两个以上不同的值、有横向合并、每格是短名称（没有分行、不超过 30 字）。表单的填写行（一整格）、带说明的行不算。"""
    values = [c for c in row if _real(c).strip()]
    return SPAN in row and len(set(values)) >= 2 and all(len(c) <= 30 and "<br>" not in c for c in values)


def _next_subtable(rows) -> int:
    """表格中间又出现“分组行（一到两行）+ 和前面相同的栏目名行”时，后一张分表从分组行起（返回行号；没有返回 0）。
    栏目名要和前面某一行的栏目名一样（按有字的格子的集合比），表单里的字段行不算。"""
    names = lambda row: frozenset(_real(c).strip() for c in row if _real(c).strip())  # noqa: E731
    seen = set()
    for i in range(len(rows) - 1):
        k = i
        while k < len(rows) and k - i < 2 and _group_row(rows[k]):
            k += 1
        if i >= 2 and k > i and k < len(rows) and _label_row(rows[k]) and names(rows[k]) in seen:
            return i
        if _label_row(rows[i]):
            seen.add(names(rows[i]))
    return 0


def _with_groups(header, groups):
    """分组行写进表头：每个分组值加在它横跨的第一列的栏目名前面（和栏目名相同的不重复），分块后每块的表头都带着
    类别名（第五轮 E3）。"""
    header = list(header)
    for row in reversed(groups):  # 上层在前：“第一类医疗器械产品目录 6801 基础外科手术器械 序号”
        for i, c in enumerate(row[:len(header)]):
            value = _real(c).strip()
            if value and value != header[i]:
                header[i] = (value + " " + header[i]).strip()
    return header


def _label_row(row) -> bool:
    """表格中间的栏目名行（“序号 | 产品名称 | 产品描述 | …”，可以有一个横跨两列的“品名举例”）：三列以上、格子都有字、
    有字的格子占六成以上（上层分组行大半是合并格）、3/4 以上是栏目名。比 is_header_row 严：表单里“产品名称 | 中文名称 |
    甲”这种字段行不当表头。"""
    named, cells = _named(row)
    return len(row) >= 3 and _full(row) and len(cells) >= 0.6 * len(row) and named >= 0.75 * len(cells)


def _filled(row):
    """横向合并跨过的格当作左边那格（只用来比较两层表头）。"""
    out = []
    for c in row:
        out.append(out[-1] if c == SPAN and out else _real(c))
    return out


def _merge_header_rows(rows: list[list[str]]) -> list[list[str]]:
    """两层表头（上一层横向合并、其余列纵向合并，带值以后前两行大半相同）并成一行：不同的列写“上层 下层”。"""
    while len(rows) > 2:
        top, below = _filled(rows[0]), _filled(rows[1])
        same = sum(1 for a, b in zip(top, below) if a == b)
        differ = [i for i, (a, b) in enumerate(zip(top, below)) if a != b]
        if same < 0.5 * len(top) or not differ or any(not top[i] or not below[i] for i in differ) \
                or not any(top.count(top[i]) > 1 for i in differ):
            break
        rows = [[a if a == b else a + " " + b for a, b in zip(top, below)]] + rows[2:]
    return rows


def _fill_merged(grid: list[list], ranges) -> None:
    """ranges: [(r0, r1, c0, c1)]（0 起、含两端）：左上角的值往下带到每一行的第一列，跨过的列记 SPAN。"""
    for r0, r1, c0, c1 in ranges:
        if r0 >= len(grid):
            continue
        value = grid[r0][c0] if c0 < len(grid[r0]) else ""
        for r in range(r0, min(r1, len(grid) - 1) + 1):
            row = grid[r]
            if len(row) <= c1:
                row.extend([""] * (c1 + 1 - len(row)))
            row[c0] = value
            for c in range(c0 + 1, c1 + 1):
                row[c] = SPAN


_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _xlsx_merges(file_path: str) -> dict[str, list[tuple[int, int, int, int]]]:
    """Sheet 名 → 合并范围（0 起、含两端），直接读包里的 XML。openpyxl 普通模式会给合并范围里每一格建对象，
    整行合并到 XFD 列（16,384 列）的文件要读几分钟，所以只用 read_only 读值、合并范围自己读。"""
    import zipfile

    from lxml import etree
    from openpyxl.utils.cell import range_boundaries

    out = {}
    with zipfile.ZipFile(file_path) as package:
        book = etree.fromstring(package.read("xl/workbook.xml"))
        rels = etree.fromstring(package.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.iter(_NS_PKG + "Relationship")}
        for sheet in book.iter(_NS_MAIN + "sheet"):
            target = targets.get(sheet.get(_NS_REL + "id")) or ""
            name = target.lstrip("/") if target.startswith("/") else "xl/" + target
            try:
                root = etree.fromstring(package.read(name))
            except KeyError:
                continue
            ranges = []
            for merge in root.iter(_NS_MAIN + "mergeCell"):
                try:
                    c0, r0, c1, r1 = range_boundaries(merge.get("ref") or "")
                except (TypeError, ValueError):
                    continue
                ranges.append((r0 - 1, r1 - 1, c0 - 1, c1 - 1))
            out[sheet.get("name")] = ranges
    return out


def _parse_xlsx(file_path: str) -> str:
    import openpyxl

    try:
        merges = _xlsx_merges(file_path)
    except Exception:  # noqa: BLE001 -- 合并范围读不出：照样读值
        merges = {}
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sections = []
    for ws in wb.worksheets:
        ws.reset_dimensions()  # 不信文件声明的范围（A1:XFD1048576），按实际存的格读
        cells = []
        for row in ws.iter_rows():
            for cell in row:
                value = cell_text(getattr(cell, "value", None))
                if value:
                    cells.append((cell.row, cell.column, value))
        if not cells:
            continue
        rows_n = max(r for r, _, _ in cells)
        cols_n = max(c for _, c, _ in cells)
        grid = [[""] * cols_n for _ in range(rows_n)]
        for r, c, value in cells:
            grid[r - 1][c - 1] = value
        _fill_merged(grid, [(r0, r1, c0, min(c1, cols_n - 1)) for r0, r1, c0, c1 in merges.get(ws.title, [])
                            if r0 < rows_n and c0 < cols_n])
        table = markdown_table(grid, titles="single")
        if table:
            sections.append((ws.title, table))
    wb.close()
    return _join_sheets(sections)


def _join_sheets(sections) -> str:
    """每个 Sheet 一节“## 名称”；只有一个 Sheet 时不写名称（“## Sheet1”不是内容）。"""
    if len(sections) == 1:
        return sections[0][1]
    return "\n\n".join(f"## {name}\n\n{table}" for name, table in sections)


def _xls_value(cell, datemode):
    import xlrd

    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, datemode)
        except (ValueError, OverflowError):
            return cell.value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return ""
    return cell.value


def _parse_xls(file_path: str) -> str:
    import xlrd

    try:
        wb = xlrd.open_workbook(file_path, formatting_info=True)  # 合并范围只有带格式信息时才读得到
    except NotImplementedError:
        wb = xlrd.open_workbook(file_path)
    sections = []
    for sheet in wb.sheets():
        grid = [[cell_text(_xls_value(sheet.cell(i, j), wb.datemode)) for j in range(sheet.ncols)]
                for i in range(sheet.nrows)]
        _fill_merged(grid, [(r0, r1 - 1, c0, c1 - 1) for r0, r1, c0, c1 in getattr(sheet, "merged_cells", [])])
        table = markdown_table(grid, titles="single")
        if table:
            sections.append((sheet.name, table))
    return _join_sheets(sections)


def excel_to_markdown(file_path: str) -> str:
    """.xls / .xlsx 全量转成 Markdown（不截断）；读不出返回空串。扩展名不是这两种时先按 xlsx 试，再按 xls。"""
    ext = Path(file_path).suffix.lower()
    readers = {".xlsx": [_parse_xlsx], ".xls": [_parse_xls]}.get(ext, [_parse_xlsx, _parse_xls])
    for reader in readers:
        try:
            return reader(file_path)
        except Exception as error:  # noqa: BLE001 -- 文件坏了记为读不出，由调用方记失败
            logger.warning(f"[Excel] {reader.__name__} 读不出 {file_path}: {error}")
    return ""


def _rows_to_markdown(rows: list[list[str]]) -> str:
    """旧的对齐表格（fetch.pagination 还在用；附件不再用它）。"""
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    col_widths = [3] * col_count
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def fmt_row(cells):
        return "| " + " | ".join(cells[i].ljust(col_widths[i]) if i < len(cells) else " " * col_widths[i]
                                 for i in range(col_count)) + " |"

    return "\n".join([fmt_row(rows[0]), "| " + " | ".join("-" * w for w in col_widths) + " |",
                      *(fmt_row(r) for r in rows[1:])])
