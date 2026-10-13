"""
标准标识符对齐客户端（T3 辅创新）单元测试。

四类场景（对应任务手册验收）：
1. 权威源命中：在线解析到 CID + 规范名
2. 未命中：在线无结果且本地白名单也无 → found=False
3. 多候选：返回候选列表、对齐置信度下调
4. 接口降级：超时 / 限流异常 → 回退本地白名单（或彻底未命中）

所有外部 HTTP 调用均被 monkeypatch 替换，不触网。
"""

import urllib.error

from core_engine.authority_client import resolve_identifier


# ───────────────── 1. 权威源命中 ─────────────────
class TestAuthorityHit:
    def test_online_hit_returns_cid_and_name(self, monkeypatch):
        monkeypatch.setattr(
            "core_engine.authority_client._fetch_cids", lambda n, t: [1234]
        )
        monkeypatch.setattr(
            "core_engine.authority_client._fetch_iupac",
            lambda c, t: "2-(acetyloxy)benzoic acid",
        )
        r = resolve_identifier("aspirin")
        assert r["found"] is True
        assert r["std_id"] == "PUBCHEM:1234"
        assert r["source_id"] == "pubchem"
        assert r["resolved_value"] == "2-(acetyloxy)benzoic acid"
        assert r["confidence"] == 0.9
        assert r["candidates"] == []
        assert r["degraded"] is False

    def test_empty_input_returns_miss(self):
        assert resolve_identifier("")["found"] is False


# ───────────────── 2. 未命中 ─────────────────
class TestAuthorityMiss:
    def test_online_miss_and_no_local(self, monkeypatch):
        monkeypatch.setattr(
            "core_engine.authority_client._fetch_cids", lambda n, t: []
        )
        r = resolve_identifier("zzznotadrug")
        assert r["found"] is False
        assert r["std_id"] is None
        assert r["confidence"] == 0.0
        assert r["degraded"] is False


# ───────────────── 3. 多候选 ─────────────────
class TestAuthorityMultiCandidate:
    def test_multi_candidate_lowers_confidence(self, monkeypatch):
        monkeypatch.setattr(
            "core_engine.authority_client._fetch_cids",
            lambda n, t: [111, 222, 333],
        )
        monkeypatch.setattr(
            "core_engine.authority_client._fetch_iupac",
            lambda c, t: f"name-{c}",
        )
        r = resolve_identifier("ambiguous")
        assert r["found"] is True
        assert len(r["candidates"]) == 3
        assert r["confidence"] == 0.6
        assert r["degraded"] is False


# ───────────────── 4. 接口降级 ─────────────────
class TestAuthorityDegradation:
    def test_online_error_falls_back_to_local(self, monkeypatch):
        def _boom(n, t):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr("core_engine.authority_client._fetch_cids", _boom)
        r = resolve_identifier("aspirin")
        assert r["found"] is True
        assert r["source_id"] == "local"
        assert r["std_id"] == "LOCAL:aspirin"
        assert r["degraded"] is True

    def test_online_error_and_no_local_returns_miss(self, monkeypatch):
        def _boom(n, t):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr("core_engine.authority_client._fetch_cids", _boom)
        r = resolve_identifier("zzznotadrug")
        assert r["found"] is False
        assert r["degraded"] is False
