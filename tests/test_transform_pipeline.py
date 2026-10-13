"""
T6 测试 —— 字段级血缘埋点（T2/T3 接入管线 + record_step）。

覆盖：
  1) L3 归一改值并写入 lineage 的 normalize 步；
  2) 标识符对齐记录 align 步 + 五元组入 authority_resolutions；
  3) process_and_store 实际串起 clean → normalize → align → quality 四步血缘。
全部不触网：resolve_identifier 一律 monkeypatch。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core_engine.transform_pipeline import apply_l3_normalization, apply_identifier_alignment


def _base():
    return {
        "uuid": "u-t6-001",
        "source_site": "nmpa.gov.cn",
        "title": "阿司匹林说明书",
        "structured_data": {"drug_name": "aspirin", "dosage": "500 mg"},
        "extra": {},
        "lineage": {},
    }


def _all_steps(parsed_result):
    return {
        e["step"]
        for entries in parsed_result.get("lineage", {}).values()
        for e in entries
    }


class TestL3NormalizationStep:
    def test_drug_name_and_dosage_normalized(self):
        out = apply_l3_normalization.fn(_base())
        assert out["structured_data"]["drug_name"] == "阿司匹林"
        assert out["structured_data"]["dosage"] == "0.5 g"

    def test_normalize_step_recorded(self):
        out = apply_l3_normalization.fn(_base())
        assert "normalize" in _all_steps(out)
        # 具体字段级血缘应包含 drug_name 的 before/after
        entries = out["lineage"].get("structured_data.drug_name", [])
        assert any(e["step"] == "normalize" and e["input"] == "aspirin" for e in entries)


class TestIdentifierAlignmentStep:
    def test_align_step_recorded_and_resolution_stored(self, monkeypatch):
        fake = {
            "found": True, "std_id": "CID:2244", "source_id": "pubchem",
            "raw_value": "aspirin", "resolved_value": "aspirin",
            "confidence": 0.9, "candidates": [], "degraded": False,
        }
        monkeypatch.setattr(
            "core_engine.transform_pipeline.resolve_identifier",
            lambda name, timeout=5.0: dict(fake, raw_value=name),
        )
        out = apply_identifier_alignment.fn(_base())
        assert out["authority_resolutions"]["aspirin"]["std_id"] == "CID:2244"
        assert "align" in _all_steps(out)
        align_entries = out["lineage"].get("drug_name", [])
        assert any(e["step"] == "align" and e["output"] == "CID:2244" for e in align_entries)

    def test_align_no_drug_name_is_safe(self):
        p = _base()
        p["structured_data"] = {}
        p["title"] = ""
        out = apply_identifier_alignment.fn(p)  # 不应抛异常
        assert out["authority_resolutions"] == {}


class TestPipelineWiring:
    def test_process_and_store_wires_normalize_and_align(self, monkeypatch):
        """process_and_store 真正串起 clean → normalize → align → quality。"""
        import core_engine.transform_pipeline as tp
        from flows import crawl_flow

        calls = []
        real_norm = tp.apply_l3_normalization.fn
        real_align = tp.apply_identifier_alignment.fn
        monkeypatch.setattr(
            tp.apply_l3_normalization, "fn",
            lambda pr: (calls.append("normalize"), real_norm(pr))[1],
        )
        monkeypatch.setattr(
            tp.apply_identifier_alignment, "fn",
            lambda pr: (calls.append("align"), real_align(pr))[1],
        )
        # 下游桩：clean 原样返回；check 直接放行；write 返回成功；quarantine 空操作
        monkeypatch.setattr(
            crawl_flow, "clean_content",
            type("T", (), {"fn": staticmethod(lambda pr: pr)})(),
        )
        monkeypatch.setattr(
            crawl_flow, "check_quality",
            type("T", (), {"fn": staticmethod(lambda pr: {**pr, "quality_passed": True})})(),
        )
        monkeypatch.setattr(
            crawl_flow, "write_to_database",
            type("T", (), {"fn": staticmethod(lambda pr: {"success": True, "status": "AUTO_PROCESSED"})})(),
        )
        monkeypatch.setattr(
            crawl_flow, "quarantine_data",
            type("T", (), {"fn": staticmethod(lambda pr: None)})(),
        )

        crawl_flow.process_and_store.fn(_base())
        assert "normalize" in calls and "align" in calls
