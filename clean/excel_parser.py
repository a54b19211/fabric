"""Excel 附件 → Markdown 表格（.xlsx 用 openpyxl 读缓存的公式值，.xls 用 xlrd）。

规则（NMPA 审计 A2/A3/A10）：
- 只取实际有字的范围：不信文件里声明的范围（有的 xlsx 声明 A1:XFD1048576，以前补成 16,384 列、29.5 万字）；
  空行去掉，首尾全空的列去掉；
- 合并单元格：值填进合并范围里的每一格，每行单独看也完整；
- 表格不补空格：`| a | b |`，分隔行 `| --- | --- |`；
- 单元格里的换行变成空格，`|` 转义成 `\\|`，一行表格永远是一行；
- 表格上方只有一个值的行（表名“2017年……统计表”，合并成整行）写成表格前的一行字，不当表头；
- 每个 Sheet 一个 `## 名称` 小标题，空 Sheet 跳过。
"""

from __future__ import annotations

import datetime as dt
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
    return " ".join(str(value).split()).replace("|", "\\|")


def _trim(rows: list[list[str]]) -> list[list[str]]:
    """去掉全空行和首尾全空的列，各行补到同样的列数。"""
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    used = [i for i in range(width) if any(r[i] for r in rows)]
    first, last = used[0], used[-1]
    return [r[first:last + 1] for r in rows]


def markdown_table(rows: list[list[str]]) -> str:
    """二维字符串 → Markdown 表格；表格上方只有一个值的行写成单独一行字。不补空格。"""
    rows = _trim(rows)
    lead = []
    while len(rows) > 1 and len(rows[0]) > 1 and len({c for c in rows[0] if c}) == 1:
        lead.append(next(c for c in rows[0] if c))
        rows = _trim(rows[1:])
    if not rows:
        return "\n\n".join(lead)
    rows = _merge_header_rows(rows)
    if len(rows[0]) == 1:  # 只有一列：不是表格，一行一段
        return "\n\n".join(lead + [r[0] for r in rows])
    width = len(rows[0])
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    # 整行合并成一格的行（表中间的小标题“总体意见建议”）：值只写在第一格，不重复整行
    lines += ["| " + " | ".join(r if len(set(r)) > 1 else [r[0]] + [""] * (width - 1)) + " |" for r in rows[1:]]
    return "\n\n".join(lead + ["\n".join(lines)])


def _merge_header_rows(rows: list[list[str]]) -> list[list[str]]:
    """两层表头（上一层横向合并、其余列纵向合并，带值以后前两行大半相同）并成一行：不同的列写“上层 下层”。"""
    while len(rows) > 2:
        top, below = rows[0], rows[1]
        same = sum(1 for a, b in zip(top, below) if a == b)
        differ = [i for i, (a, b) in enumerate(zip(top, below)) if a != b]
        if same < 0.5 * len(top) or not differ or any(not top[i] or not below[i] for i in differ) \
                or not any(top.count(top[i]) > 1 for i in differ):
            break
        rows = [[a if a == b else a + " " + b for a, b in zip(top, below)]] + rows[2:]
    return rows


def _fill_merged(grid: list[list], ranges) -> None:
    """ranges: [(r0, r1, c0, c1)]（0 起、含两端），左上角的值填满整个范围。"""
    for r0, r1, c0, c1 in ranges:
        if r0 >= len(grid):
            continue
        value = grid[r0][c0] if c0 < len(grid[r0]) else ""
        for r in range(r0, min(r1, len(grid) - 1) + 1):
            row = grid[r]
            if len(row) <= c1:
                row.extend([""] * (c1 + 1 - len(row)))
            for c in range(c0, c1 + 1):
                row[c] = value


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
        table = markdown_table(grid)
        if table:
            sections.append(f"## {ws.title}\n\n{table}")
    wb.close()
    return "\n\n".join(sections)


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
        table = markdown_table(grid)
        if table:
            sections.append(f"## {sheet.name}\n\n{table}")
    return "\n\n".join(sections)


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
