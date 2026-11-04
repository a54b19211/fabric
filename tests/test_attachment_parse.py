"""加工层附件解析（processing.attachment_parse）：复用旧 ETL 的 MinerU / doc 转 PDF / Excel 解析，
结果按（附件版本, 处理器版本）存；失败是带原因的状态，不抛出、不冒充“附件为空”。不联网。"""

import hashlib

import pytest

from processing import attachment_parse as ap

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


def test_mineru_endpoint_is_configurable(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_MINERU_URL", "http://127.0.0.1:9/")
    assert ap.mineru_base() == "http://127.0.0.1:9"
