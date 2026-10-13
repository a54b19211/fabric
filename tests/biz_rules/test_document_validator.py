"""
T5 测试 —— 文档级结构化校验 validate_document()。

覆盖三类违规（required / type / enum）与一条合规文档；
并验证 storage_writer.write_to_database 在落库后返回 violation_count。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from biz_rules.document_validator import validate_document, ViolationReport


def _base_doc(**overrides):
    doc = {
        "uuid": "u-001",
        "title": "某药品说明书",
        "raw_content": "正文内容……",
        "publish_date": "2024-01-01",
        "category": "drug_label",
        "doc_type": "drug_label",
        "language": "zh",
        "status": "AUTO_PROCESSED",
        "summary": "摘要",
    }
    doc.update(overrides)
    return doc


class TestRequiredViolation:
    def test_missing_uuid(self):
        r = validate_document(_base_doc(uuid=""))
        assert not r.valid
        assert any(v["field"] == "uuid" and v["rule"] == "required" for v in r.violations)

    def test_missing_title_and_raw_content(self):
        r = validate_document(_base_doc(title="", raw_content=None))
        fields = {v["field"] for v in r.violations if v["rule"] == "required"}
        assert {"title", "raw_content"} <= fields


class TestTypeViolation:
    def test_wrong_type(self):
        r = validate_document(_base_doc(uuid=12345))  # uuid 应为 str
        assert any(v["field"] == "uuid" and v["rule"] == "type" for v in r.violations)


class TestEnumViolation:
    def test_illegal_doc_type(self):
        r = validate_document(_base_doc(doc_type="unknown_type"))
        assert any(v["field"] == "doc_type" and v["rule"] == "enum" for v in r.violations)

    def test_empty_enum_not_flagged(self):
        # 枚举字段为空时不判 enum（交由 required 处理）
        r = validate_document(_base_doc(doc_type="", language=""))
        assert not any(v["rule"] == "enum" for v in r.violations)


class TestCleanDocument:
    def test_valid_doc_has_no_violation(self):
        r = validate_document(_base_doc())
        assert r.valid
        assert r.violation_count == 0
        assert r.violations == []


class TestStorageReturn:
    def test_write_returns_violation_count(self, monkeypatch):
        """write_to_database 应把 violation_count 透传到返回 dict。"""
        from core_engine import storage_writer

        # 构造一个合规 envelope，使落库与校验都可控
        captured = {}

        def fake_upsert(env):
            captured["env"] = env
            return True

        monkeypatch.setattr("db.repository.upsert_document", fake_upsert)
        monkeypatch.setattr("core_engine.storage_writer.upsert_document", fake_upsert)
        monkeypatch.setattr(
            "core_engine.storage_writer.save_lineage", lambda pr: None
        )
        monkeypatch.setattr("config.settings.APP.ENABLE_LOCAL_STORAGE", False)

        parsed = {
            "uuid": "u-storage-001",
            "source_site": "nmpa.gov.cn",
            "source_url": "https://www.nmpa.gov.cn/doc/1",
            "structured_data": {},
            "markdown": "足够长的正文内容。" * 20,
            "title": "某药品批准信息",
            "doc_type": "drug_label",
            "language": "zh",
        }
        res = storage_writer.write_to_database.fn(parsed)
        assert res["violation_count"] == 0
        assert res["success"] is True
