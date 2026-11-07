"""
Excel 解析器 —— 将 .xls / .xlsx 转换为 Markdown 表格。

依赖：
    openpyxl >= 3.1  （读 .xlsx）
    xlrd >= 2.0      （读 .xls，仅支持旧版二进制格式）

跨平台：纯 Python，无需 Office / LibreOffice。

转换规则：
    - 每个 Sheet 生成一个 Markdown 二级标题 + 表格
    - 空 Sheet 跳过
    - 单元格值统一转字符串，None/空值显示为空字符串
    - 全量读取，不截断——分块由下游 flows/chunking.py 负责
"""

from pathlib import Path

from loguru import logger

# ── 内部工具 ──────────────────────────────────────────────────────────

def _cell_str(value) -> str:
    """单元格值转字符串，处理 None 和特殊类型。"""
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else str(value)
    return str(value).strip()


def _rows_to_markdown(rows: list[list[str]]) -> str:
    """将二维字符串列表转为 Markdown 表格。"""
    if not rows:
        return ""

    col_count = max(len(r) for r in rows)
    col_widths = [3] * col_count
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        padded = [cells[i].ljust(col_widths[i]) if i < len(cells)
                  else " " * col_widths[i]
                  for i in range(col_count)]
        return "| " + " | ".join(padded) + " |"

    lines = [fmt_row(rows[0])]
    lines.append("| " + " | ".join("-" * w for w in col_widths) + " |")
    for row in rows[1:]:
        lines.append(fmt_row(row))

    return "\n".join(lines)


# ── xlsx 解析（openpyxl）─────────────────────────────────────────────

def _parse_xlsx(file_path: str) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sections = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        all_rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            str_row = [_cell_str(c) for c in row]
            if any(s for s in str_row):
                all_rows.append(str_row)

        if not all_rows:
            continue

        sections.append(f"## {sheet_name}\n\n{_rows_to_markdown(all_rows)}")

    wb.close()
    return "\n\n".join(sections)


# ── xls 解析（xlrd）──────────────────────────────────────────────────

def _parse_xls(file_path: str) -> str:
    import xlrd

    wb = xlrd.open_workbook(file_path)
    sections = []

    for sheet in wb.sheets():
        all_rows: list[list[str]] = []
        for i in range(sheet.nrows):
            str_row = [_cell_str(sheet.cell_value(i, j)) for j in range(sheet.ncols)]
            if any(s for s in str_row):
                all_rows.append(str_row)

        if not all_rows:
            continue

        sections.append(f"## {sheet.name}\n\n{_rows_to_markdown(all_rows)}")

    return "\n\n".join(sections)


# ── 对外接口 ──────────────────────────────────────────────────────────

def excel_to_markdown(file_path: str) -> str:
    """
    将 .xls / .xlsx 文件全量转换为 Markdown 表格字符串。
    不做行数截断，分块由 flows/chunking.py 负责。
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    try:
        if ext == ".xlsx":
            logger.info(f"[Excel] openpyxl 解析: {file_path}")
            result = _parse_xlsx(file_path)
        elif ext == ".xls":
            logger.info(f"[Excel] xlrd 解析: {file_path}")
            result = _parse_xls(file_path)
        else:
            # 未知扩展名，先试 xlsx，失败再试 xls
            logger.info(f"[Excel] 未知扩展名 {ext}，尝试 xlsx 解析: {file_path}")
            try:
                result = _parse_xlsx(file_path)
            except Exception as e:
                logger.warning(f"[Excel] xlsx 解析失败，降级 xls: {e}")
                result = _parse_xls(file_path)

        if result:
            logger.info(f"[Excel] 解析完成，Markdown 长度: {len(result)}")
        else:
            logger.warning(f"[Excel] 解析结果为空: {file_path}")
        return result

    except Exception as e:
        logger.error(f"[Excel] 解析失败: {file_path} | {e}")
        return ""

