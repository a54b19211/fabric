"""加工层附件解析（clean.attachment_parse）：复用旧 ETL 的 MinerU / doc 转 PDF / Excel 解析，
结果按（附件版本, 处理器版本）存；失败是带原因的状态，不抛出、不冒充“附件为空”。不联网。"""

import hashlib

import pytest

from clean import attachment_parse as ap

PDF = b"%PDF-1.4\nsynthetic\n%%EOF\n"
DOC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"synthetic doc"


def stored(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    return {"store_path": str(path), "sha256": hashlib.sha256(content).hexdigest(), "file_ext": "." + name.rsplit(".", 1)[1]}


@pytest.fixture
def tools(monkeypatch):
    calls = []

    def mineru(path):
        calls.append(("mineru", path))
        return {"success": True, "markdown": "# 附件标题\n\n第 1 页内容。", "error": ""}

    monkeypatch.setattr(ap, "_mineru", mineru)
    monkeypatch.setattr(ap, "_doc_to_pdf", lambda path: calls.append(("convert", path)) or path + ".pdf")
    monkeypatch.setattr(ap, "_excel", lambda path: calls.append(("excel", path)) or "| a | b |\n|---|---|\n| 1 | 2 |")
    return calls


def test_pdf_goes_to_mineru(tmp_path, tools):
    out = ap.parse_file(**stored(tmp_path, "x.pdf", PDF))
    assert out["status"] == "parsed" and out["parse_method"] == "mineru"
    assert out["text"].startswith("# 附件标题") and out["chars"] == len(out["text"])
    assert [c[0] for c in tools] == ["mineru"]


def test_docx_goes_to_self_hosted_mineru_natively(tmp_path, tools):
    out = ap.parse_file(**stored(tmp_path, "x.docx", b"PK synthetic docx"))
    assert out["status"] == "parsed" and out["parse_method"] == "mineru"
    assert [c[0] for c in tools] == ["mineru"]  # 自部署 3.4 原生支持 docx，不转 PDF


def test_doc_goes_to_cloud_mineru_when_a_token_is_configured(tmp_path, tools, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_MINERU_CLOUD_TOKEN", "synthetic")
    monkeypatch.setattr(ap, "_mineru_cloud", lambda path: tools.append(("cloud", path)) or
                        {"success": True, "markdown": "# 云端解析", "error": ""})
    out = ap.parse_file(**stored(tmp_path, "x.doc", DOC))
    assert out["status"] == "parsed" and out["parse_method"] == "mineru_cloud"
    assert [c[0] for c in tools] == ["cloud"]


def test_doc_without_cloud_token_is_converted_then_self_hosted(tmp_path, tools, monkeypatch):
    monkeypatch.delenv("DATAFABRIC_MINERU_CLOUD_TOKEN", raising=False)
    out = ap.parse_file(**stored(tmp_path, "x.doc", DOC))
    assert out["status"] == "parsed" and out["parse_method"] == "libreoffice_pdf+mineru"
    assert [c[0] for c in tools] == ["convert", "mineru"]


def test_self_hosted_failure_falls_back_to_cloud(tmp_path, tools, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_MINERU_CLOUD_TOKEN", "synthetic")
    monkeypatch.setattr(ap, "_mineru", lambda path: {"success": False, "markdown": "", "error": "busy"})
    monkeypatch.setattr(ap, "_mineru_cloud", lambda path: {"success": True, "markdown": "# 云端", "error": ""})
    out = ap.parse_file(**stored(tmp_path, "x.pdf", PDF))
    assert out["parse_method"] == "mineru_cloud" and out["attempts"] == ["mineru:parser_failed", "mineru_cloud:ok"]


def test_excel_is_read_directly(tmp_path, tools):
    out = ap.parse_file(**stored(tmp_path, "x.xlsx", b"PK\x03\x04 synthetic"))
    assert out["status"] == "parsed" and out["parse_method"] == "excel" and "| 1 | 2 |" in out["text"]


def test_bin_extension_is_sniffed_from_bytes(tmp_path, tools):
    out = ap.parse_file(**stored(tmp_path, "x.bin", PDF))
    assert out["parse_method"] == "mineru"


def test_digest_mismatch_and_missing_original_are_failures_with_reason(tmp_path, tools):
    meta = stored(tmp_path, "x.pdf", PDF)
    meta["sha256"] = "0" * 64
    out = ap.parse_file(**meta)
    assert out["status"] == "failed" and out["error"] == "original_digest_mismatch"
    missing = ap.parse_file(store_path=str(tmp_path / "nope.pdf"), sha256="0" * 64, file_ext=".pdf")
    assert missing["status"] == "failed" and missing["error"] == "original_missing"
    assert tools == []  # 原件不对，不调解析服务


def test_parser_failure_and_empty_text_are_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "_mineru", lambda path: {"success": False, "markdown": "", "error": "timeout"})
    monkeypatch.delenv("DATAFABRIC_MINERU_CLOUD_TOKEN", raising=False)
    out = ap.parse_file(**stored(tmp_path, "x.pdf", PDF))
    assert out["status"] == "failed" and out["error"] == "parser_failed" and out["text"] == ""
    monkeypatch.setattr(ap, "_mineru", lambda path: {"success": True, "markdown": "  \n", "error": ""})
    assert ap.parse_file(**stored(tmp_path, "y.pdf", PDF))["error"] == "empty_text"

    def boom(path):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ap, "_mineru", boom)
    assert ap.parse_file(**stored(tmp_path, "z.pdf", PDF))["error"] == "parser_failed"


def test_unknown_type_is_unsupported(tmp_path, tools):
    out = ap.parse_file(**stored(tmp_path, "x.bin", b"\x00\x01\x02\x03\x04\x05\x06\x07\x08 random"))
    assert out["status"] == "unsupported" and tools == []


def test_a_stored_web_page_and_an_archive_are_unsupported_and_say_what_they_are(tmp_path, tools):
    import io
    import zipfile

    page = ap.parse_file(**stored(tmp_path, "x.bin", b"<!DOCTYPE html>\r\n<html lang=\"zh\"><body>news</body></html>"))
    assert (page["status"], page["error"], page["file_ext"]) == ("unsupported", "html_page_not_a_file", ".html")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.pdf", "x")
    packed = ap.parse_file(**stored(tmp_path, "y.bin", archive.getvalue()))
    assert (packed["status"], packed["file_ext"]) == ("unsupported", ".zip") and tools == []  # not sent as a Word file


def test_a_large_file_with_next_to_no_text_is_a_scan_that_needs_ocr(tmp_path, monkeypatch):
    """Live: NMPA .docx files of 4-9 MB parsed to the two characters of their heading, a 94 KB PDF to six."""
    monkeypatch.setattr(ap, "_mineru", lambda path: {
        "success": True, "markdown": "附件", "error": "",
        "content_list": [{"type": "text", "text": "附件", "page_idx": 0}, {"type": "image", "page_idx": 0}]})
    scan = ap.parse_file(**stored(tmp_path, "scan.pdf", PDF + b"\x00" * 60_000))
    assert (scan["status"], scan["error"], scan["text"]) == ("unsupported", "text_layer_missing", "")
    assert scan["attempts"] == ["mineru:ok", "text_layer_missing"] and scan["parse_method"] == "mineru"
    small = ap.parse_file(**stored(tmp_path, "note.pdf", PDF))  # a short note in a small file is just short
    assert small["status"] == "parsed" and "附件" in small["text"]


def test_mineru_endpoint_is_configurable(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_MINERU_URL", "http://127.0.0.1:9/")
    assert ap.mineru_base() == "http://127.0.0.1:9"


# ——v2：定位与大小上限（Codex r59）——


def test_pdf_blocks_become_text_with_page_markers_and_offsets(tmp_path, monkeypatch):
    blocks = [
        {"type": "text", "text": "附件1", "page_idx": 0},
        {"type": "page_number", "text": "1", "page_idx": 0},
        {"type": "table", "table_caption": ["注销目录"], "table_body": "<table><tr><td>阿莫西林</td></tr></table>",
         "page_idx": 1},
    ]
    monkeypatch.setattr(ap, "_mineru", lambda p: {"success": True, "markdown": "md", "error": "", "content_list": blocks})
    out = ap.parse_file(**stored(tmp_path, "x.pdf", PDF))
    assert out["locator_kind"] == "pdf_page"
    assert out["text"].startswith("〔第1页〕") and "〔第2页〕" in out["text"]
    assert "页码" not in out["text"] and out["text"].count("1") == out["text"].count("附件1") + 1  # 页码块已跳过
    for seg in out["segments"]:
        piece = out["text"][seg["start"]:seg["end"]]
        assert piece and ("附件1" in piece or "阿莫西林" in piece)
    assert [s["page"] for s in out["segments"]] == [1, 2]


def test_docx_native_parse_has_no_pages_only_blocks(tmp_path, monkeypatch):
    blocks = [{"type": "text", "text": "第一段", "page_idx": 0}, {"type": "text", "text": "第二段", "page_idx": 0}]
    monkeypatch.setattr(ap, "_mineru", lambda p: {"success": True, "markdown": "md", "error": "", "content_list": blocks})
    out = ap.parse_file(**stored(tmp_path, "x.docx", b"PK\x03\x04 docx"))
    assert out["locator_kind"] == "block" and "〔第" not in out["text"]
    assert all("page" not in s for s in out["segments"])


def test_converted_pages_are_labelled_as_converted(tmp_path, tools, monkeypatch):
    monkeypatch.delenv("DATAFABRIC_MINERU_CLOUD_TOKEN", raising=False)
    monkeypatch.setattr(ap, "_mineru", lambda p: {"success": True, "markdown": "md", "error": "",
                                                  "content_list": [{"type": "text", "text": "正文", "page_idx": 0}]})
    out = ap.parse_file(**stored(tmp_path, "x.doc", DOC))
    assert out["locator_kind"] == "converted_page"


def test_no_content_list_means_locator_unavailable(tmp_path, tools):
    out = ap.parse_file(**stored(tmp_path, "x.pdf", PDF))
    assert out["status"] == "parsed" and out["locator_kind"] == "unavailable" and out["segments"] == []


def test_excel_is_located_by_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "_excel", lambda p: "## 名单\n\n| a |\n\n## 附表\n\n| b |")
    out = ap.parse_file(**stored(tmp_path, "x.xls", b"\xd0\xcf\x11\xe0 xls"))
    assert out["locator_kind"] == "sheet" and [s["sheet"] for s in out["segments"]] == ["名单", "附表"]


def test_too_large_is_refused_before_hashing_or_parsing(tmp_path, tools, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_ATTACHMENT_MAX_PARSE_BYTES", "4")
    out = ap.parse_file(**stored(tmp_path, "x.pdf", PDF))
    assert out["status"] == "failed" and out["error"] == "too_large" and tools == []


def test_cloud_mode_skips_self_hosted_mineru(monkeypatch):
    from clean import attachment_parse as ap
    monkeypatch.setenv("DATAFABRIC_MINERU_CLOUD_TOKEN", "t")
    monkeypatch.setenv("DATAFABRIC_MINERU_MODE", "cloud")
    assert [m for m, _, _ in ap._plan(".pdf")] == ["mineru_cloud"]
    assert [m for m, _, _ in ap._plan(".docx")] == ["mineru_cloud"]
    monkeypatch.delenv("DATAFABRIC_MINERU_MODE")
    assert [m for m, _, _ in ap._plan(".pdf")] == ["mineru", "mineru_cloud"]
