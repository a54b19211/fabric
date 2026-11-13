"""本地文件原件导入（来源 local_files）：上传接口 → 原件按 sha256 落盘 → 附件版本 → 加工层解析。

不连库：import_archive 换成替身（只做和真函数一样的全包投影校验）；MinerU 换成替身。PDF 是真实的小 PDF。
"""

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pdf_fixture import make_pdf

from api.routers import history as history_router
from clean import attachment_parse as ap
from fetch import catalog, local_files
from fetch import web_client as client
from fetch.sources import PLUGINS, SOURCE_CATALOG, project_record, read_public
from store import history as store
from store.history_models import FetchRequest, HistoryError

ADMIN = "test-admin-only"
PDF = make_pdf("Local import evidence")


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAFABRIC_HISTORY_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["local_files"]')
    monkeypatch.delenv("DATAFABRIC_LOCAL_FILE_MAX_BYTES", raising=False)
    monkeypatch.setattr(client.APP, "DATA_ROOT", tmp_path)
    imported = []

    def import_archive(request, key):
        # 与 store.history.import_archive 相同的第一步：全包先投影校验
        prepared = [(item, project_record(request.source_id, item)) for item in request.records]
        imported.append({"request": request, "key": key, "prepared": prepared})
        return {
            "id": "run-" + key, "source_id": request.source_id, "is_demo": request.is_demo, "kind": "archive",
            "state": "completed", "attempt": 1, "created_at": datetime(2026, 11, 4, tzinfo=timezone.utc),
            "scope_description": request.scope_description, "version_ids": ["0" * 64], "coverage": {},
            "attempts": [],
        }

    monkeypatch.setattr(store, "import_archive", import_archive)
    app = FastAPI()
    app.include_router(history_router.router)
    return TestClient(app), imported, tmp_path


def upload(test_client, content, name="报告.pdf", key="k1", collection="t28-evidence", token=ADMIN, demo="true"):
    return test_client.post(
        "/api/history/local-files",
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": key},
        files={"file": (name, content, "application/octet-stream")},
        data={"collection": collection, "is_demo": demo},
    )


def stored_files(root):
    folder = Path(root) / "raw" / "local_files"
    return sorted(folder.iterdir()) if folder.exists() else []


def test_catalog_declares_local_files_as_an_import_source_with_license_entry():
    spec = catalog.spec("local_files")
    assert spec.status == "active" and spec.plugin == "local_file_import" and not spec.verified
    assert "local_files" in catalog.FETCHABLE and "local_files" not in catalog.WATCHABLE
    assert PLUGINS["local_file_import"] is not None
    assert SOURCE_CATALOG["local_files"]["acquisition_channel"] == "institutional_upload"
    from clean import derived_text

    assert catalog.SITE_SOURCES < catalog.ATTACHMENT_SOURCES == derived_text.ATTACHMENT_SOURCES
    assert {"local_files", "pubmed"} <= derived_text.ATTACHMENT_SOURCES
    # 白名单只放加工层能解析的格式
    assert set(local_files.SIGNATURES) <= ap.SUPPORTED


def test_real_pdf_upload_stores_the_original_by_sha256_and_builds_an_attachment_version(api):
    test_client, imported, root = api
    response = upload(test_client, PDF)
    assert response.status_code == 200, response.text
    assert response.json()["source_id"] == "local_files"
    request, prepared = imported[0]["request"], imported[0]["prepared"]
    assert request.source_id == "local_files" and request.is_demo and request.declared_count == 1
    item, projected = prepared[0]
    assert item.source_record_id == "local-file:t28-evidence/报告.pdf"
    data = json.loads(item.raw_content)
    digest = hashlib.sha256(PDF).hexdigest()
    assert data["role"] == "attachment" and data["site"] == "local_files" and data["file_ext"] == ".pdf"
    assert data["sha256"] == digest and data["size"] == len(PDF) and data["parent_record_id"] is None
    path = Path(data["store_path"])
    assert path.parent == root / "raw" / "local_files" and path.name.endswith("." + digest + ".pdf")
    assert path.read_bytes() == PDF
    facts = projected["facts"]
    assert facts["role"] == "attachment" and facts["sha256"] == digest and facts["store_path"] == str(path)
    assert facts["access"]["tier"] == "uploaded_original" and facts["collection"] == "t28-evidence"
    assert projected["source_url"] is None and projected["parser_version"] == "datafabric.attachment.access.v1"


def test_same_bytes_reuse_the_original_and_new_bytes_are_a_new_version_of_the_same_record(api):
    test_client, imported, root = api
    assert upload(test_client, PDF, key="a").status_code == 200
    assert upload(test_client, PDF, key="b").status_code == 200
    assert len(stored_files(root)) == 1  # 同字节只存一份，不覆盖
    other = make_pdf("Second revision")
    assert upload(test_client, other, key="c").status_code == 200
    ids = {entry["prepared"][0][0].source_record_id for entry in imported}
    shas = {entry["prepared"][0][1]["facts"]["sha256"] for entry in imported}
    assert ids == {"local-file:t28-evidence/报告.pdf"} and len(shas) == 2
    assert len(stored_files(root)) == 2


def test_stored_original_goes_through_the_attachment_parser_with_page_locators(api, monkeypatch):
    test_client, imported, _ = api
    assert upload(test_client, PDF).status_code == 200
    facts = imported[0]["prepared"][0][1]["facts"]
    seen = []

    def mineru(path):
        seen.append(Path(path).read_bytes())
        return {"success": True, "markdown": "Local import evidence",
                "content_list": [{"type": "text", "text": "Local import evidence", "page_idx": 0}]}

    monkeypatch.setattr(ap, "_mineru", mineru)
    monkeypatch.delenv("DATAFABRIC_MINERU_CLOUD_TOKEN", raising=False)
    result = ap.parse_file(store_path=facts["store_path"], sha256=facts["sha256"], file_ext=facts["file_ext"])
    assert seen == [PDF]  # 解析的是落盘原件的副本
    assert result["status"] == "parsed" and result["locator_kind"] == "pdf_page"
    assert result["segments"][0]["page"] == 1 and "Local import evidence" in result["text"]


@pytest.mark.parametrize(
    "name, content, code",
    [
        ("notice.pdf", b"<html>not a pdf</html>", "media_mismatch"),
        ("page.html", b"<html></html>", "unsupported_media"),
        ("run.exe", b"MZ\x90\x00", "unsupported_media"),
        ("table.docx", b"PK\x03\x04broken", "media_mismatch"),
    ],
)
def test_type_whitelist_checks_extension_and_header_and_stores_nothing(api, name, content, code):
    test_client, imported, root = api
    response = upload(test_client, content, name=name)
    assert response.status_code == 415 and response.json()["detail"]["code"] == code
    assert not imported and not stored_files(root)


def test_docx_with_word_part_is_accepted(api):
    test_client, imported, _ = api
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    assert upload(test_client, buffer.getvalue(), name="memo.docx").status_code == 200
    assert imported[0]["prepared"][0][1]["facts"]["file_ext"] == ".docx"


def test_size_limit_credentials_collection_and_source_permission(api, monkeypatch):
    test_client, imported, root = api
    monkeypatch.setenv("DATAFABRIC_LOCAL_FILE_MAX_BYTES", "100")
    assert upload(test_client, PDF).status_code == 413
    monkeypatch.delenv("DATAFABRIC_LOCAL_FILE_MAX_BYTES")
    monkeypatch.setenv("DATAFABRIC_LOCAL_FILE_MAX_BYTES", str(10**12))
    assert local_files.max_upload_bytes() == local_files.ROUTE_BUDGET  # 不超过路由 16MiB 预算
    assert upload(test_client, PDF, token="wrong").status_code == 403
    response = upload(test_client, PDF, collection="../x")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_collection"
    monkeypatch.setenv("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", '["pubmed"]')
    assert upload(test_client, PDF).status_code == 403
    assert not imported and not stored_files(root)


def test_client_paths_in_file_names_are_dropped():
    assert local_files.clean_name("C:\\Users\\x\\a.pdf") == "a.pdf"
    assert local_files.clean_name("dir/b.pdf") == "b.pdf"
    with pytest.raises(HistoryError):
        local_files.clean_name("..")


# ——收件目录：/fetch source_id=local_files query=inbox——


@pytest.fixture
def inbox(monkeypatch, tmp_path):
    folder = tmp_path / "inbox"
    (folder / "batch").mkdir(parents=True)
    (folder / "batch" / "a.pdf").write_bytes(PDF)
    (folder / "batch" / "b.pdf").write_bytes(make_pdf("B"))
    (tmp_path / "outside.pdf").write_bytes(PDF)
    (folder / "batch" / "link.pdf").symlink_to(tmp_path / "outside.pdf")
    monkeypatch.setenv("DATAFABRIC_LOCAL_IMPORT_DIR", str(folder))
    monkeypatch.setattr(client.APP, "DATA_ROOT", tmp_path / "data")
    return folder


def fetch(ids, **extra):
    return read_public(FetchRequest(source_id="local_files", query="inbox", source_record_ids=ids, **extra))


def test_inbox_plugin_reads_listed_files_through_the_same_store_and_version_shape(inbox):
    batch = fetch(["batch/a.pdf", "batch/missing.pdf", "batch/link.pdf"])
    assert batch["error"] is None and batch["requests"] == 0 and batch["bytes"] == len(PDF)
    [record] = batch["records"]
    assert record.source_record_id == "local-file:inbox/batch/a.pdf"
    projected = project_record("local_files", record)
    assert projected["facts"]["discovered_from"] == "local_inbox"
    assert Path(projected["facts"]["store_path"]).read_bytes() == PDF
    statuses = [(c["path"], c["status"]) for c in batch["attachment_coverage"]]
    assert statuses == [("batch/a.pdf", "stored"), ("batch/missing.pdf", "missing"), ("batch/link.pdf", "missing")]


def test_inbox_budget_and_path_traversal(inbox):
    batch = fetch(["batch/a.pdf", "batch/b.pdf"], max_records=1)
    assert batch["stop_reason"] == "record_budget" and len(batch["records"]) == 1
    assert batch["attachment_coverage"][-1] == {"record_id": "local-file:inbox/batch/b.pdf", "path": "batch/b.pdf",
                                                "status": "not_attempted"}
    for bad in ("../outside.pdf", "/etc/passwd", "batch\\a.pdf"):
        failed = fetch([bad])
        assert failed["stop_reason"] == "source_failed" and failed["error"]["code"] == "invalid_file_name"
        assert not failed["records"]


def test_projection_rejects_a_record_whose_identity_does_not_match(api):
    test_client, imported, _ = api
    assert upload(test_client, PDF).status_code == 200
    item = imported[0]["prepared"][0][0]
    forged = item.model_copy(update={"source_record_id": "local-file:t28-evidence/other.pdf"})
    with pytest.raises(HistoryError) as exc:
        project_record("local_files", forged)
    assert exc.value.code == "native_id_mismatch"
