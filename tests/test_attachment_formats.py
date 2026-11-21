"""附件解析 v4（NMPA 审计 A1–A11）：表格不补空格、合并格带值、Word 编号和标题、文本框、修订、PDF 先读文字层、
HTML 表格转 Markdown、完整性校验。真文件在 tests/fixtures/attachments（NMPA 公开附件），其余是现做的小文件。不联网。"""

import hashlib
import io
import zipfile
from pathlib import Path

import openpyxl
import pytest

from clean import attachment_parse as ap
from clean import office_text
from clean.excel_parser import excel_to_markdown, markdown_table
from tests.pdf_fixture import make_pdf

FIXTURES = Path(__file__).parent / "fixtures" / "attachments"
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def stored(path: Path) -> dict:
    content = path.read_bytes()
    return {"store_path": str(path), "sha256": hashlib.sha256(content).hexdigest(), "file_ext": path.suffix}


@pytest.fixture
def no_mineru(monkeypatch):
    def refuse(path):
        raise AssertionError("MinerU 不该被调用")

    monkeypatch.setattr(ap, "_mineru", refuse)
    monkeypatch.setattr(ap, "_mineru_cloud", refuse)
    monkeypatch.delenv("DATAFABRIC_MINERU_MODE", raising=False)


# ── Excel（A2/A3/A10）──


def test_markdown_table_has_no_padding_and_title_row_becomes_text():
    rows = [["2017年统计表", "2017年统计表", ""], ["序号", "名称", ""], ["1", "甲", ""], ["", "", ""]]
    assert markdown_table(rows, titles="single") == "2017年统计表\n\n| 序号 | 名称 |\n| --- | --- |\n| 1 | 甲 |"  # Excel


def test_xlsx_declared_whole_sheet_merged_cells_and_newlines(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "表1"
    ws.append(["反馈表"])
    ws.append(["序号", "章节", "意见"])
    ws.append([1, "第一章", "第一行\n第二行 | 竖线"])
    ws.append([2, None, "无"])
    ws.merge_cells("A1:C1")
    ws.merge_cells("B3:B4")
    raw = io.BytesIO()
    wb.save(raw)
    # 照 NMPA 附件3 的样子：声明范围是整张表，还有一个合并到 XFD 列的整行合并（openpyxl 普通模式会读几分钟）
    src, out = zipfile.ZipFile(raw), io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b'<dimension ref="A1:C4"/>', b'<dimension ref="A1:XFD1048576"/>')
                data = data.replace(b'<mergeCell ref="A1:C1"/>', b'<mergeCell ref="A1:XFD1"/>')
            dst.writestr(item, data)
    path = tmp_path / "x.xlsx"
    path.write_bytes(out.getvalue())
    md = excel_to_markdown(str(path))
    # 只有一个 Sheet：不写“## 表1”；表名整行合并只写一次；纵向合并往下带
    assert md == ("反馈表\n\n| 序号 | 章节 | 意见 |\n| --- | --- | --- |\n"
                  "| 1 | 第一章 | 第一行<br>第二行 \\| 竖线 |\n| 2 | 第一章 | 无 |")  # 格内换行 <br>（N3）


def test_excel_two_row_title_once_horizontal_merge_once_and_no_header_row():
    from clean.excel_parser import SPAN, _join_sheets, _lead_rows

    lead, rows = _lead_rows([["名单", SPAN, SPAN], ["名单", SPAN, SPAN], ["序号", "名称", "备注"]])
    assert lead == ["名单"] and rows == [["序号", "名称", "备注"]]  # 表名纵向合并两行：只写一次
    assert markdown_table([["序号", "申请分类", SPAN, SPAN], ["1", "□ 甲", "", "x"]]).splitlines()[0] == \
        "| 序号 | 申请分类 |  |  |"  # 横向合并：值只写一次，不是“申请分类 | 申请分类 | 申请分类”
    assert markdown_table([["心脏疾病", "心肌缺血", "未知"], ["精神异常", "失眠", "未知"]]).splitlines()[:3] == [
        "| 心脏疾病 | 心肌缺血 | 未知 |", "| --- | --- | --- |", "| 精神异常 | 失眠 | 未知 |"]  # 没有栏目名行：第一行当表头，不补“列1”（第四轮 N2）
    assert _join_sheets([("Sheet1", "| a |")]) == "| a |" and _join_sheets([("甲", "x"), ("乙", "y")]) == \
        "## 甲\n\nx\n\n## 乙\n\ny"


# ── Word（A4/A5/A8）──


def _docx(path, body, numbering=None, styles=None):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", f"<w:document {W} {MC}><w:body>{body}</w:body></w:document>")
        if numbering:
            package.writestr("word/numbering.xml", f"<w:numbering {W}>{numbering}</w:numbering>")
        if styles:
            package.writestr("word/styles.xml", f"<w:styles {W}>{styles}</w:styles>")
    return str(path)


def _p(text, ppr=""):
    return f"<w:p><w:pPr>{ppr}</w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>"


def _tc(text, tcpr=""):
    return f"<w:tc><w:tcPr>{tcpr}</w:tcPr>{_p(text)}</w:tc>"


NUMBERING = (
    '<w:abstractNum w:abstractNumId="0">'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="chineseCounting"/><w:lvlText w:val="%1、"/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="chineseCounting"/><w:lvlText w:val="（%2）"/></w:lvl>'
    '</w:abstractNum><w:num w:numId="3"><w:abstractNumId w:val="0"/></w:num>')
STYLES = '<w:style w:styleId="H2"><w:name w:val="heading 2"/></w:style>'


def test_docx_numbering_headings_merged_cells_textbox_and_revisions(tmp_path):
    num = '<w:numPr><w:ilvl w:val="{}"/><w:numId w:val="3"/></w:numPr>'
    textbox = ('<w:p><w:r><w:t>批号</w:t></w:r><w:r><mc:AlternateContent><mc:Choice><w:drawing><w:txbxContent>'
               + _p("包括但不限于产品批号") + '</w:txbxContent></w:drawing></mc:Choice><mc:Fallback><w:pict>'
               '<w:txbxContent>' + _p("包括但不限于产品批号") + '</w:txbxContent></w:pict></mc:Fallback>'
               '</mc:AlternateContent></w:r></w:p>')
    body = (
        _p("修订要求", '<w:pStyle w:val="H2"/>')
        + _p("警示语应包含：", num.format(0)) + _p("本品含盐酸克仑特罗。", num.format(1))
        + _p("不良反应：", num.format(0))
        + '<w:p><w:r><w:t>保留</w:t></w:r><w:del><w:r><w:delText>删掉的字</w:delText></w:r></w:del></w:p>'
        + "<w:tbl>"
        + "<w:tr>" + _tc("药品名称") + _tc("批号") + _tc("检验机构") + "</w:tr>"
        + "<w:tr>" + _tc("加替沙星滴眼液", '<w:vMerge w:val="restart"/>') + _tc("2509161E")
        + _tc("天津市药品检验研究院", '<w:vMerge w:val="restart"/>') + "</w:tr>"
        + "<w:tr>" + _tc("", "<w:vMerge/>") + _tc("2601271E") + _tc("", "<w:vMerge/>") + "</w:tr>"
        + "<w:tr>" + _tc("合计两批", '<w:gridSpan w:val="2"/>') + _tc("—") + "</w:tr>"
        + "<w:tr><w:tc>" + textbox + "</w:tc>" + _tc("x") + _tc("y") + "</w:tr>"
        + "</w:tbl>"
    )
    out = office_text.parse_docx(_docx(tmp_path / "a.docx", body, NUMBERING, STYLES))
    texts = [b.get("text") or b.get("table_body") for b in out["content_list"]]
    # 标题样式的短行、“一、”编号短行是小标题，级别按编号样式（## 起，# 留给附件名）；带句号的“（一）……。”是正文
    assert texts[:5] == ["## 修订要求", "### 一、警示语应包含：", "（一）本品含盐酸克仑特罗。", "### 二、不良反应：",
                         "保留"]
    table = texts[5].splitlines()
    assert table[0] == "| 药品名称 | 批号 | 检验机构 |"
    assert table[2:5] == ["| 加替沙星滴眼液 | 2509161E | 天津市药品检验研究院 |",
                          "| 加替沙星滴眼液 | 2601271E | 天津市药品检验研究院 |",
                          "| 合计两批 |  | — |"]  # 横向合并只在第一格写值
    assert table[5] == "| 批号 | x | y |"  # 文本框的字不并进格子
    assert texts[6:] == ["包括但不限于产品批号"]  # 文本框单独一块，兼容副本不重复
    assert "删掉的字" not in out["markdown"] and out["reference_chars"] > 0


def test_real_nmpa_docx_with_vertically_merged_drug_name(no_mineru):
    """国家药品监督管理局2026年第39号通告附件1.docx：加替沙星滴眼液一格纵向合并三行（审计 ypggtg B_0）。"""
    out = ap.parse_file(**stored(FIXTURES / "nmpa_2026_39_attachment1.docx"))
    assert out["status"] == "parsed" and out["parse_method"] == "office_docx"
    rows = [line for line in out["text"].splitlines() if line.startswith("| 加替沙星滴眼液 |")]
    assert len(rows) >= 3 and all("郑州卓峰制药有限公司" in r or "楚雄老拨云堂药业有限公司" in r for r in rows)
    assert "  " not in out["text"] and "〔第" not in out["text"]


# ── PDF（A1/A11）──


def test_pdf_text_layer_is_read_first_and_has_no_page_markers(tmp_path, no_mineru):
    path = tmp_path / "x.pdf"
    path.write_bytes(make_pdf("Notice on drug quality sampling results of the first batch"))
    out = ap.parse_file(**stored(path))
    assert out["status"] == "parsed" and out["parse_method"] == "pypdf_text" and out["attempts"] == ["pypdf_text:ok"]
    assert "Notice on drug quality" in out["text"] and "〔第" not in out["text"]
    assert out["segments"][0]["page"] == 1 and out["locator_kind"] == "pdf_page"


def test_pdf_without_usable_text_layer_goes_to_mineru(tmp_path, monkeypatch):
    monkeypatch.delenv("DATAFABRIC_MINERU_MODE", raising=False)
    monkeypatch.setattr(ap, "_mineru", lambda p: {"success": True, "markdown": "扫描件正文", "error": ""})
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_pdf("x"))
    out = ap.parse_file(**stored(path))
    assert out["parse_method"] == "mineru"
    assert out["attempts"] == ["pypdf_text:parser_failed:no_usable_text_layer", "mineru:ok"]


def test_pdf_scan_with_mineru_off_is_text_layer_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_MINERU_MODE", "off")
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_pdf("x"))
    out = ap.parse_file(**stored(path))
    assert (out["status"], out["error"]) == ("unsupported", "text_layer_missing")


def test_real_sichuan_pdf_table_is_read_cell_by_cell(no_mineru):
    """四川局 2021 年第 7 号通告（审计 fxjzh D3）：MinerU 识别成“通 牛”“维染膏葡酒”，落款丢失；文字层是对的。"""
    out = ap.parse_file(**stored(FIXTURES / "sichuan_2021_7.pdf"))
    assert out["parse_method"] == "pypdf_text"
    text = out["text"]
    assert "四川省药品监督管理局\n\n2021 年 5 月 8 日" in text
    row = next(line for line in text.splitlines() if line.startswith("| 1 |"))
    for cell in ("SJD20070030", "凯维斯染发膏（葡萄酒红）", "广州市凯维斯化妆品有限公司", "30ml+30ml/盒",
                 "绵阳市市场监督管理局", "四川省药品检验研究院"):
        assert f"| {cell} |" in row
    assert "- 2 -" not in text


# ── tidy（A7）与完整性（A6）──


def test_html_table_becomes_markdown_with_spans_and_private_bullets_are_removed():
    html = ('<table><tr><td>药品名称</td><td>批号</td></tr><tr><td rowspan="2">加替沙星</td><td>210903</td></tr>'
            '<tr><td>210904</td></tr><tr><td colspan="2">合计</td></tr></table>')
    assert ap.tidy("前\n" + html + "\n\uf0b7后\uf0a8\ue004") == (
        "前\n\n| 药品名称 | 批号 |\n| --- | --- |\n| 加替沙星 | 210903 |\n| 加替沙星 | 210904 |\n| 合计 |  |"
        "\n\n• 后□")  # 私用区：已知的项目符号、勾选框换成可见符号，段落标记（U+E004）去掉


def test_mineru_heading_levels_are_kept():
    text, _, _ = ap.from_blocks([{"type": "text", "text": "第一章 总则", "text_level": 1, "page_idx": 0}], "pdf_page")
    assert text == "# 第一章 总则"


def test_result_far_shorter_than_the_reference_is_incomplete_not_parsed(tmp_path, monkeypatch):
    """审计 fgwj C2：一个 15 页 .doc 只解析出附件 2，仍标“成功”。"""
    monkeypatch.setenv("DATAFABRIC_MINERU_MODE", "off")
    monkeypatch.setattr(ap, "_office", lambda p: {"success": True, "markdown": "附件2", "error": "",
                                                  "content_list": [{"type": "text", "text": "附件2"}],
                                                  "reference_chars": 4000})
    path = tmp_path / "x.docx"
    path.write_bytes(b"PK synthetic")
    out = ap.parse_file(**stored(path))
    assert out["status"] == "incomplete" and out["error"] == "incomplete:chars:3/4000"
    assert out["attempts"] == ["office_docx:incomplete:chars:3/4000"]


def test_old_versions_are_not_reused():
    assert ap.REUSABLE_VERSIONS == () and ap.PROCESSOR_VERSION == "datafabric.attachment_parse.v9"
