"""本机 Word 读法（clean.office_text）：.docx 直读 document.xml，段落一块、表格一块 Markdown 表格。不联网。"""

import shutil
import subprocess
import zipfile

import pytest

from clean import office_text

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DOCUMENT = (
    f'<w:document {W}><w:body>'
    '<w:p><w:r><w:t>附件1</w:t></w:r></w:p>'
    '<w:p><w:r><w:t>医疗器械</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>注册自检</w:t></w:r><w:r><w:t>指南</w:t></w:r></w:p>'
    '<w:p/>'
    '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>序号</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>品种</w:t></w:r></w:p></w:tc></w:tr>'
    '<w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>甲</w:t></w:r><w:r><w:br/>'
    '<w:t>乙</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    '<w:p><w:r><w:t>第一行</w:t></w:r><w:r><w:br/><w:t>第二行</w:t></w:r></w:p>'
    '</w:body></w:document>'
)


def make_docx(path):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", DOCUMENT)
    return str(path)


def test_docx_paragraphs_and_tables_in_document_order(tmp_path):
    out = office_text.parse_docx(make_docx(tmp_path / "a.docx"))
    assert out["success"]
    kinds = [b["type"] for b in out["content_list"]]
    assert kinds == ["text", "text", "table", "text"]
    assert out["content_list"][1]["text"] == "医疗器械注册自检指南"  # 一段里的几个 run 接在一起
    assert out["content_list"][2]["table_body"].splitlines()[2].split("|")[2].strip() == "甲 乙"
    assert out["content_list"][3]["text"] == "第一行\n第二行"


def test_not_a_docx_is_a_failure_not_an_exception(tmp_path):
    path = tmp_path / "x.docx"
    path.write_bytes(b"PK not really")
    assert office_text.parse_docx(str(path)) == {"success": False, "markdown": "", "error": "not_a_docx"}


@pytest.mark.skipif(not shutil.which("soffice"), reason="没有 LibreOffice")
def test_doc_is_converted_by_libreoffice_then_read(tmp_path):
    source = tmp_path / "src.html"
    source.write_text("<html><body><p>医疗器械注册自检指南</p></body></html>", encoding="utf-8")
    subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp_path}/p", "--headless", "--convert-to",
                    "doc:MS Word 97", "--outdir", str(tmp_path), str(source)], check=True, capture_output=True,
                   timeout=180)
    out = office_text.parse_doc(str(tmp_path / "src.doc"))
    assert out["success"] and "医疗器械注册自检指南" in out["markdown"]
