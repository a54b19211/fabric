"""
T3 分族标识符对齐——格式合法性校验层单元测试（H 项第一阶段）。

覆盖五类离线标识符族的格式校验：
    - cas_number            CAS 登记号（结构 + 校验位）
    - application_number    NDA/ANDA/BLA 上市申请号
    - nct_id                ClinicalTrials.gov 登记号
    - pmid                  PubMed 文献号
    - doi                   DOI

全部为本地纯函数，不触网、不连库。校验语义仅「格式合法」，不确认是否真实
存在于权威库（权威源交叉核对属后续阶段），故 source_id 恒为 "format"。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core_engine.authority_client import resolve_identifier


def _fmt(r):
    """断言格式校验层的公共返回特征。"""
    assert r["source_id"] == "format"
    assert r["degraded"] is False
    assert r["candidates"] == []
    assert r["confidence"] == 0.9


# ───────────────── 1. CAS 登记号 ─────────────────
class TestCasNumber:
    def test_valid_cas_with_correct_checksum(self):
        r = resolve_identifier("50-00-0", id_type="cas_number")
        assert r["found"] is True
        assert r["std_id"] == "CAS:50-00-0"
        assert r["resolved_value"] == "50-00-0"
        _fmt(r)

    def test_acetaminophen_cas(self):
        r = resolve_identifier("103-90-2", id_type="cas_number")
        assert r["found"] is True
        assert r["std_id"] == "CAS:103-90-2"

    def test_wrong_checksum_is_rejected(self):
        # 50-00-0 的校验位是 0，改成 1 必须判非法
        r = resolve_identifier("50-00-1", id_type="cas_number")
        assert r["found"] is False
        assert r["std_id"] is None
        assert r["source_id"] == "none"

    def test_malformed_structure_is_rejected(self):
        assert resolve_identifier("50-0", id_type="cas_number")["found"] is False
        assert resolve_identifier("CAS50-00-0", id_type="cas_number")["found"] is False


# ───────────────── 2. NDA/ANDA/BLA 申请号 ─────────────────
class TestApplicationNumber:
    def test_nda(self):
        r = resolve_identifier("NDA 123456", id_type="application_number")
        assert r["found"] is True
        assert r["std_id"] == "NDA:123456"
        assert r["resolved_value"] == "123456"
        _fmt(r)

    def test_anda_lowercase_with_dash(self):
        r = resolve_identifier("anda-202347", id_type="application_number")
        assert r["found"] is True
        assert r["std_id"] == "ANDA:202347"

    def test_bla(self):
        r = resolve_identifier("BLA125327", id_type="application_number")
        assert r["found"] is True
        assert r["std_id"] == "BLA:125327"

    def test_five_digit_number_rejected(self):
        assert resolve_identifier("NDA 12345", id_type="application_number")["found"] is False

    def test_unknown_prefix_rejected(self):
        assert resolve_identifier("IND 123456", id_type="application_number")["found"] is False


# ───────────────── 3. NCT 登记号 ─────────────────
class TestNctId:
    def test_valid_nct(self):
        r = resolve_identifier("NCT01234567", id_type="nct_id")
        assert r["found"] is True
        assert r["std_id"] == "NCT:01234567"
        assert r["resolved_value"] == "01234567"
        _fmt(r)

    def test_lowercase_nct(self):
        r = resolve_identifier("nct01234567", id_type="nct_id")
        assert r["found"] is True
        assert r["std_id"] == "NCT:01234567"

    def test_seven_digit_rejected(self):
        assert resolve_identifier("NCT1234567", id_type="nct_id")["found"] is False

    def test_non_digit_rejected(self):
        assert resolve_identifier("NCTABCDEFGH", id_type="nct_id")["found"] is False


# ───────────────── 4. PMID ─────────────────
class TestPmid:
    def test_valid_pmid(self):
        r = resolve_identifier("32450107", id_type="pmid")
        assert r["found"] is True
        assert r["std_id"] == "PMID:32450107"
        _fmt(r)

    def test_single_digit_pmid(self):
        assert resolve_identifier("7", id_type="pmid")["found"] is True

    def test_over_eight_digit_rejected(self):
        assert resolve_identifier("123456789", id_type="pmid")["found"] is False

    def test_letters_rejected(self):
        assert resolve_identifier("PMID32450107", id_type="pmid")["found"] is False


# ───────────────── 5. DOI ─────────────────
class TestDoi:
    def test_valid_doi(self):
        r = resolve_identifier("10.1038/s41586-020-2649-2", id_type="doi")
        assert r["found"] is True
        assert r["std_id"] == "DOI:10.1038/s41586-020-2649-2"
        assert r["resolved_value"] == "10.1038/s41586-020-2649-2"
        _fmt(r)

    def test_missing_doi_prefix_rejected(self):
        assert resolve_identifier("s41586-020-2649-2", id_type="doi")["found"] is False

    def test_short_registrant_rejected(self):
        # registrant 需 4 位以上
        assert resolve_identifier("10.1/abc", id_type="doi")["found"] is False


# ───────────────── 6. 路由边界 ─────────────────
class TestRouting:
    def test_empty_input_with_id_type(self):
        r = resolve_identifier("", id_type="nct_id")
        assert r["found"] is False
        assert r["std_id"] is None

    def test_unknown_id_type_raises(self):
        with pytest.raises(ValueError):
            resolve_identifier("NCT01234567", id_type="nctid")

    def test_drug_name_unchanged_default(self, monkeypatch):
        """默认 id_type 仍走 PubChem 路径，不受分族改动影响。"""
        monkeypatch.setattr(
            "core_engine.authority_client._fetch_cids", lambda n, t: [1234]
        )
        monkeypatch.setattr(
            "core_engine.authority_client._fetch_iupac",
            lambda c, t: "acetylsalicylic acid",
        )
        r = resolve_identifier("aspirin")
        assert r["source_id"] == "pubchem"
        assert r["std_id"] == "PUBCHEM:1234"
