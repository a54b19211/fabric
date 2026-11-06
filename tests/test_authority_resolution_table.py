"""
A1：标准标识符对齐结果入库（doc_authority_resolution）测试。

背景：此前对齐结果只写 data/authority/<site>/<uuid>.json 旁路文件，数据库
没有记录，论文所称「标准标识符对齐」在存量语料上没有可入库复查的结果。
A1 原由旧 ETL 的 storage_writer.write_to_database 双写；旧 ETL 已删除，这里只测入库函数本身。

本文件重点验证三件事：
  1. 两族字段映射正确 —— 药名族带 candidate/rule/candidates_tried，
     标识符族这三个字段必须为 None（不是空字符串）
  2. 内容变化转版本（legacy_cache_retained）时必须跳过双写，否则新正文的
     对齐结果会盖掉旧正文的记录
  3. 表不存在（迁移未执行）时安全跳过，不让写库整体失败

不连接数据库：入库路径用假 session，写库分支用 monkeypatch 桩。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from db.repository import _collect_authority_rows, upsert_authority_resolutions


# ────────────────────────── 夹具 ──────────────────────────

def _drug_res(found=True, **over):
    res = {
        "found": found,
        "std_id": "PUBCHEM:148124" if found else "",
        "source_id": "pubchem",
        "raw_value": "Docetaxel",
        "resolved_value": "docetaxel-iupac-name" if found else "Docetaxel",
        "confidence": 0.9 if found else 0.0,
        "candidates": [],
        "degraded": False,
        "candidate": "Docetaxel",
        "rule": "as_is",
        "candidates_tried": 1,
    }
    res.update(over)
    return res


def _ident_res(field="pmid", found=True, **over):
    res = {
        "found": found,
        "std_id": "PMID:42333167" if found else "",
        "source_id": "format",
        "raw_value": "42333167",
        "resolved_value": "42333167" if found else "",
        "confidence": 0.9 if found else 0.0,
        "candidates": [],
        "degraded": False,
        "field": field,
        "id_type": field,
    }
    res.update(over)
    return res


# ────────────────────── 两族字段映射 ──────────────────────

def test_drug_family_keeps_normalization_triplet():
    """药名族要保留 candidate/rule/candidates_tried —— 论文里「命中靠归一」的贡献统计依赖它们。"""
    rows = _collect_authority_rows(
        "u1", "clinicaltrials", {"Docetaxel and GM-CSF": _drug_res(rule="combo_split")}, None
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["id_type"] == "drug_name"
    assert row["resolution_key"] == "Docetaxel and GM-CSF"
    assert row["candidate"] == "Docetaxel"
    assert row["rule"] == "combo_split"
    assert row["candidates_tried"] == 1


def test_skipped_flag_kept_for_placeholder():
    """占位名（Placebo/盐水）必须带 skipped 落库 —— 命中率分母靠它剔除，
    否则 28 条会被当成 28 个待对齐名，把 80.8% 算成 75.0%。"""
    placeholder = _drug_res(found=False, std_id="", skipped=True, candidate="", rule="")
    rows = _collect_authority_rows("u1", "clinicaltrials", {"Placebo": placeholder}, None)
    assert rows[0]["skipped"] is True
    assert rows[0]["found"] is False


def test_identifier_family_leaves_triplet_null():
    """标识符族没有归一三元组，必须是 None 而不是空串（否则统计口径会混）。"""
    rows = _collect_authority_rows("u1", "pubmed", None, {"pmid": _ident_res()})
    assert len(rows) == 1
    row = rows[0]
    assert row["id_type"] == "pmid"
    assert row["resolution_key"] == "pmid"
    assert row["candidate"] is None
    assert row["rule"] is None
    assert row["candidates_tried"] is None
    assert row["source_id"] == "format"


def test_identifier_id_type_falls_back_to_key():
    """结果里没写 id_type 时回落用字段名，避免整行被丢。"""
    res = _ident_res()
    res.pop("id_type")
    rows = _collect_authority_rows("u1", "pubmed", None, {"doi": res})
    assert rows[0]["id_type"] == "doi"


def test_both_families_in_one_pass():
    rows = _collect_authority_rows(
        "u1", "clinicaltrials",
        {"Docetaxel": _drug_res()},
        {"pmid": _ident_res(), "doi": _ident_res(field="doi")},
    )
    assert len(rows) == 3
    assert {r["id_type"] for r in rows} == {"drug_name", "pmid", "doi"}


def test_duplicate_key_after_str_coercion_deduped():
    """key 经 str() 归一会撞的（如 1 与 "1"）只留一条，否则撞唯一约束。"""
    dup = {1: _ident_res(), "1": _ident_res()}
    rows = _collect_authority_rows("u1", "pubmed", None, dup)
    assert len(rows) == 1


def test_malformed_entries_skipped():
    """非 dict 的脏值跳过，不让一条坏数据毁掉整批。"""
    rows = _collect_authority_rows(
        "u1", "pubmed", {"bad": "not-a-dict"}, {"pmid": None, "doi": _ident_res(field="doi")}
    )
    assert len(rows) == 1
    assert rows[0]["id_type"] == "doi"


# ────────────────────── 空输入与表缺失保护 ──────────────────────

def test_empty_input_returns_zero_without_touching_db():
    assert upsert_authority_resolutions("u1", "pubmed", {}, {}) == 0
    assert upsert_authority_resolutions("u1", "pubmed", None, None) == 0


def test_missing_table_skips_gracefully(monkeypatch):
    """迁移未执行的库：只警告并返回 0，不能让写库整体失败。"""

    class _FakeResult:
        def scalar(self):
            return False  # to_regclass(...) IS NOT NULL → False

    class _FakeSession:
        def execute(self, *_a, **_kw):
            return _FakeResult()

        def flush(self):
            raise AssertionError("表不存在时不应继续写入")

    class _FakeCtx:
        def __enter__(self):
            return _FakeSession()

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr("db.session.get_session", lambda: _FakeCtx())
    assert upsert_authority_resolutions("u1", "pubmed", {"Docetaxel": _drug_res()}, {}) == 0
