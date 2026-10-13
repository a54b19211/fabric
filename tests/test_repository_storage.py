"""
T1 基线测试 —— 锁定 write_to_database() 落库链路输出结构。

快照目的：后续 T3/T4 以及 C 组落库链路改动时，本文件的断言需随之更新。
DB 写动作（upsert_document）与血缘落盘（save_lineage）均被 mock，不产生外部副作用。

说明（对应开题报告口径）：当前落库直接搬运、置信度来自来源硬编码查表，属前期探索性
原型暴露的不足；本测试仅固化现状以便回归。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core_engine import storage_writer
from core_engine.storage_writer import write_to_database


def _parsed(uuid="u-storage-001", source_url="https://www.nmpa.gov.cn/x", site="nmpa"):
    return {
        "uuid": uuid,
        "source_site": site,
        "markdown": "正文内容 " * 50,
        "structured_data": {
            "title": "T",
            "publish_date": "",
            "category": "",
            "summary": "",
        },
        "sniffed_meta": {},
        "source_url": source_url,
        "attachment_links": [],
        "extra": {},
        "lineage": {},
    }


def test_write_produces_envelope_and_calls_upsert(monkeypatch):
    """落库返回结构正确，且 upsert_document 被调用一次、信封 uuid 一致。"""
    captured = {}

    def _fake_upsert(envelope):
        captured["env"] = envelope

    monkeypatch.setattr(storage_writer, "upsert_document", _fake_upsert)
    monkeypatch.setattr(storage_writer, "save_lineage", lambda pr: None)
    monkeypatch.setattr(storage_writer.APP, "ENABLE_LOCAL_STORAGE", False)

    res = write_to_database.fn(_parsed())

    # 返回结构
    assert res["uuid"] == "u-storage-001"
    assert res["success"] is True
    # nmpa 为已知官方站点 → 置信度 0.95 → 自动通过审核
    assert res["confidence_score"] == 0.95
    assert res["status"] == "AUTO_PROCESSED"

    # 落库动作被调用一次，且信封字段正确
    assert captured["env"] is not None
    assert captured["env"].uuid == "u-storage-001"
    assert captured["env"].confidence.score == 0.95
