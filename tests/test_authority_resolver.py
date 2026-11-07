"""
多源标识符解析与冲突消解单元测试（T3 第二阶段 · K3）。

覆盖：
    1. 四类消解策略（多数投票 / 证据加权 / 来源可信度分层 / 结构判据优先）
    2. 全降级 → degraded=True（未命中≠不存在）、明确未命中 → degraded=False
    3. Platt scaling 置信度校准
    4. 权威源适配器降级（不触网，monkeypatch _http_get_json）

全部纯函数 / monkeypatch，不连接数据库、不发起真实网络请求。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core_engine import authority_resolver as ar
from core_engine.authority_resolver import (
    _query_gsrs,
    _query_icd11,
    _query_ols4,
    _query_uniprot,
    evidence_weighted,
    majority_vote,
    platt_predict,
    platt_scale,
    resolve_conflict,
    source_hierarchy,
    structural_criterion,
)


def _res(found, std_id, source_id, confidence=0.9, raw="aspirin", degraded=False):
    return {
        "found": found, "std_id": std_id, "source_id": source_id,
        "raw_value": raw, "resolved_value": std_id,
        "confidence": confidence, "candidates": [], "degraded": degraded,
    }


# ───────────────── 1. 多数投票 ─────────────────
class TestMajorityVote:
    def test_majority_wins(self):
        rs = [
            _res(True, "PUBCHEM:2244", "pubchem"),
            _res(True, "PUBCHEM:2244", "uniprot"),
            _res(True, "UNII:XXXX", "gsrs"),
        ]
        out = majority_vote(rs)
        assert out["found"] is True
        assert out["std_id"] == "PUBCHEM:2244"
        assert out["candidates"][0] == {"std_id": "PUBCHEM:2244", "votes": 2}
        # 得票占比 2/3 × 平均置信度 0.9
        assert out["confidence"] == pytest.approx(round((2 / 3) * 0.9, 4))

    def test_all_miss_returns_not_found(self):
        out = majority_vote([_res(False, None, "none", raw="x")])
        assert out["found"] is False
        assert out["degraded"] is False

    def test_all_degraded_marks_unknown(self):
        rs = [_res(False, None, "uniprot", degraded=True, raw="x"),
              _res(False, None, "ols4", degraded=True, raw="x")]
        out = majority_vote(rs)
        assert out["found"] is False
        assert out["degraded"] is True


# ───────────────── 2. 证据加权 ─────────────────
class TestEvidenceWeighted:
    def test_weighted_evidence_wins(self):
        rs = [
            _res(True, "PUBCHEM:2244", "pubchem", confidence=0.6),
            _res(True, "UNII:XXXX", "gsrs", confidence=0.9),
        ]
        out = evidence_weighted(rs)
        # gsrs 权重 1.2×0.9=1.08 > pubchem 1.0×0.6=0.6
        assert out["std_id"] == "UNII:XXXX"

    def test_consensus_confidence_is_share(self):
        rs = [
            _res(True, "PUBCHEM:2244", "pubchem", confidence=0.9),
            _res(True, "PUBCHEM:2244", "uniprot", confidence=0.9),
        ]
        out = evidence_weighted(rs)
        assert out["confidence"] == pytest.approx(1.0)


# ───────────────── 3. 来源可信度分层 ─────────────────
class TestSourceHierarchy:
    def test_higher_tier_beats_lower(self):
        rs = [
            _res(True, "PUBCHEM:2244", "pubchem", confidence=0.99),  # tier 1
            _res(True, "UNII:XXXX", "gsrs", confidence=0.5),         # tier 0
        ]
        out = source_hierarchy(rs)
        assert out["std_id"] == "UNII:XXXX"  # 官方源优先，即使置信度低

    def test_same_tier_higher_confidence_wins(self):
        rs = [
            _res(True, "PUBCHEM:2244", "pubchem", confidence=0.5),
            _res(True, "OLS4:X", "ols4", confidence=0.9),  # 同 tier 1
        ]
        out = source_hierarchy(rs)
        assert out["std_id"] == "OLS4:X"


# ───────────────── 4. 结构判据优先 ─────────────────
class TestStructuralCriterion:
    def test_checksum_verified_cas_beats_free_text(self):
        rs = [
            _res(True, "LOCAL:aspirin", "local", confidence=1.0),
            _res(True, "CAS:50-00-0", "gsrs", confidence=0.5),
        ]
        out = structural_criterion(rs)
        assert out["std_id"] == "CAS:50-00-0"  # 结构最强，尽管置信度低

    def test_same_structure_higher_confidence(self):
        rs = [
            _res(True, "PUBCHEM:2244", "pubchem", confidence=0.5),
            _res(True, "UNIPROT:P0001", "uniprot", confidence=0.9),
        ]
        out = structural_criterion(rs)
        assert out["std_id"] == "UNIPROT:P0001"


# ───────────────── 5. 策略路由 ─────────────────
class TestResolveConflict:
    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError):
            resolve_conflict([_res(True, "X", "pubchem")], strategy="nope")


# ───────────────── 6. Platt 校准 ─────────────────
class TestPlattScale:
    def test_separable_scores_calibrate_to_extremes(self):
        scores = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        labels = [0, 0, 0, 1, 1, 1]
        a, b = platt_scale(scores, labels)
        probs = platt_predict(scores, a, b)
        assert probs[0] < 0.3
        assert probs[-1] > 0.7

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            platt_scale([0.1, 0.2], [0])


# ───────────────── 7. 权威源适配器降级 ─────────────────
class TestAdapters:
    def test_uniprot_hit(self, monkeypatch):
        monkeypatch.setattr(
            ar, "_http_get_json",
            lambda url, t: {"results": [{
                "primaryAccession": "P05305",
                "proteinDescription": {"recommendedName": {"fullName": {"value": "Endothelin-1"}}},
            }]},
        )
        r = _query_uniprot("endothelin")
        assert r["found"] is True
        assert r["std_id"] == "UNIPROT:P05305"
        assert r["source_id"] == "uniprot"

    def test_uniprot_network_error_degrades(self, monkeypatch):
        import urllib.error

        def _boom(url, t):
            raise urllib.error.URLError("down")

        monkeypatch.setattr(ar, "_http_get_json", _boom)
        r = _query_uniprot("aspirin")
        assert r["found"] is False
        assert r["degraded"] is True
        assert r["source_id"] == "uniprot"

    def test_ols4_hit(self, monkeypatch):
        monkeypatch.setattr(
            ar, "_http_get_json",
            lambda url, t: {"response": {"docs": [{"obo_id": "CHEBI:15365", "label": "aspirin"}]}},
        )
        r = _query_ols4("aspirin")
        assert r["std_id"] == "OLS4:CHEBI:15365"

    def test_gsrs_not_deployed_degrades(self):
        # 默认 GSRS_BASE 为空 → 直接降级，不触网
        assert ar.GSRS_BASE == ""
        r = _query_gsrs("aspirin")
        assert r["found"] is False
        assert r["degraded"] is True
        assert r["source_id"] == "gsrs"

    def test_icd11_not_deployed_degrades(self):
        assert ar.ICD11_BASE == ""
        r = _query_icd11("diabetes")
        assert r["degraded"] is True
        assert r["source_id"] == "icd11"


# ───────────────── 8. 多源入口回退 ─────────────────
class TestMultiEntry:
    def test_non_drug_family_falls_back_to_format(self):
        # nct_id 不参与多源裁决，走格式校验层
        r = ar.resolve_identifier_multi("NCT01234567", id_type="nct_id")
        assert r["std_id"] == "NCT:01234567"
        assert r["source_id"] == "format"
