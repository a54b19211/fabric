"""A0.8 —— 双权威库分族：PubChem（小分子）+ ChEMBL（生物制品）。

问题（2026-10-27 实测，PubChem 单库）：
    28 个待对齐药名命中 15 个（53.6%）。未命中的 11 个里有 5 个是生物制品：
        Nivolumab / Ipilimumab / Filgrastim / Pegfilgrastim / Etanercept
    PubChem 的 compound 库面向小分子，蛋白质类药物通常只有 substance 记录，
    按名称查 CID 直接 404。ChEMBL 是生物活性分子库，这 5 个全部有精确
    pref_name（预检：pref_name__iexact 5/5 命中，且对 'Texas' 返回 0 条）。

本文件锁定的行为：
    1. PubChem 命中即返回，不再请求 ChEMBL（省一次外部调用）
    2. PubChem 确认未命中（非降级）→ 查 ChEMBL，命中则 std_id=CHEMBL:<id>
    3. PubChem 降级（网络/限流）→ **不查** ChEMBL（换库也修不好网络，只会翻倍等待）
    4. ChEMBL 用精确名匹配（pref_name__iexact），不做全文模糊检索
       —— 防止单 token 常见词（地名/企业名）被模糊命中，制造假成功
    5. ChEMBL 也未命中 → found=False 且 degraded=False（有效负例，进命中率分母）
    6. 本地白名单仍在两个在线库之后（它只是接口不可用时的兜底，不是权威源）

本文件不触网：全部通过 monkeypatch 替换 HTTP 层。
"""

import urllib.error

import pytest

from core_engine import authority_client
from core_engine.authority_client import resolve_identifier

# conftest 的 autouse fixture 会把 authority_client._fetch_chembl_molecule 换成
# 「永远不命中」的桩（保证单测不触网）。要在本文件里直接验证这个函数本身，
# 必须在 fixture 生效前就把真实引用绑好 —— 否则拿到的是桩。
from core_engine.authority_client import _fetch_chembl_molecule as real_fetch_chembl

# 真实 ChEMBL 响应片段（2026-10-27 预检 Nivolumab 返回的字段结构）
CHEMBL_NIVOLUMAB = {
    "molecule_chembl_id": "CHEMBL2108738",
    "pref_name": "NIVOLUMAB",
    "molecule_type": "Antibody",
}


def _no_cids(_name, _timeout):
    return []


def _boom(_name, _timeout):
    raise urllib.error.URLError("connection reset")


# ── 分族顺序与停表条件 ────────────────────────────────────────────────


def test_pubchem_hit_short_circuits_chembl(monkeypatch):
    """PubChem 命中后不应再请求 ChEMBL。"""
    calls = {"chembl": 0}

    monkeypatch.setattr(authority_client, "_fetch_cids", lambda n, t: [2244])
    monkeypatch.setattr(authority_client, "_fetch_iupac", lambda c, t: "acetylsalicylic acid")

    def _count(_name, _timeout):
        calls["chembl"] += 1
        return None

    monkeypatch.setattr(authority_client, "_fetch_chembl_molecule", _count)

    res = resolve_identifier("aspirin")
    assert res["std_id"] == "PUBCHEM:2244"
    assert calls["chembl"] == 0


def test_pubchem_miss_falls_through_to_chembl(monkeypatch):
    """生物制品场景：PubChem 没有，ChEMBL 有 → 对齐成功。"""
    monkeypatch.setattr(authority_client, "_fetch_cids", _no_cids)
    monkeypatch.setattr(
        authority_client, "_fetch_chembl_molecule", lambda n, t: dict(CHEMBL_NIVOLUMAB)
    )

    res = resolve_identifier("Nivolumab")
    assert res["found"] is True
    assert res["std_id"] == "CHEMBL:CHEMBL2108738"
    assert res["source_id"] == "chembl"
    assert res["resolved_value"] == "NIVOLUMAB"
    assert res["degraded"] is False


def test_pubchem_degraded_skips_chembl(monkeypatch):
    """PubChem 降级时不查 ChEMBL：换库解决不了网络故障。"""
    calls = {"chembl": 0}

    monkeypatch.setattr(authority_client, "_fetch_cids", _boom)

    def _count(_name, _timeout):
        calls["chembl"] += 1
        return dict(CHEMBL_NIVOLUMAB)

    monkeypatch.setattr(authority_client, "_fetch_chembl_molecule", _count)

    res = resolve_identifier("Nivolumab")
    assert calls["chembl"] == 0
    assert res["found"] is False
    assert res["degraded"] is True  # 没查成，不进命中率分母


def test_both_miss_is_definitive_negative(monkeypatch):
    """两个库都确认没有 → 有效负例（degraded=False），计入分母。"""
    monkeypatch.setattr(authority_client, "_fetch_cids", _no_cids)
    monkeypatch.setattr(authority_client, "_fetch_chembl_molecule", lambda n, t: None)

    res = resolve_identifier("JNJ-26866138")
    assert res["found"] is False
    assert res["degraded"] is False
    assert res["source_id"] == "none"


def test_local_whitelist_comes_after_chembl(monkeypatch):
    """本地白名单是兜底，不能抢在权威库前面。"""
    monkeypatch.setattr(authority_client, "_fetch_cids", _no_cids)
    monkeypatch.setattr(
        authority_client, "_fetch_chembl_molecule", lambda n, t: dict(CHEMBL_NIVOLUMAB)
    )
    # aspirin 在本地白名单里，但 ChEMBL 若命中应优先采用权威结果
    res = resolve_identifier("aspirin")
    assert res["source_id"] == "chembl"

    # 两个库都不可用时才落到白名单（且标记 degraded，因为没查成）
    monkeypatch.setattr(authority_client, "_fetch_cids", _boom)
    fb = resolve_identifier("aspirin")
    assert fb["source_id"] == "local"
    assert fb["degraded"] is True


# ── 查询构造：必须精确匹配 ────────────────────────────────────────────


def test_chembl_query_uses_exact_name_filter(monkeypatch):
    """必须用 pref_name__iexact，禁止全文检索（防假阳性）。"""
    seen = {}

    def _fake_get(url, _timeout):
        seen["url"] = url
        return {"molecules": []}

    monkeypatch.setattr(authority_client, "_http_get_json", _fake_get)
    monkeypatch.setattr(authority_client, "_CHEMBL_MIN_INTERVAL", 0)

    assert real_fetch_chembl("Nivolumab", 5.0) is None
    assert "pref_name__iexact=Nivolumab" in seen["url"]
    assert "molecule/search" not in seen["url"]


def test_chembl_empty_result_returns_none(monkeypatch):
    """ChEMBL 无匹配时返回 HTTP 200 + 空列表（不是 404），要正确识别。"""
    monkeypatch.setattr(
        authority_client, "_http_get_json", lambda url, t: {"molecules": []}
    )
    monkeypatch.setattr(authority_client, "_CHEMBL_MIN_INTERVAL", 0)
    assert real_fetch_chembl("Texas", 5.0) is None


def test_chembl_skips_molecule_without_id(monkeypatch):
    """响应里若有缺 molecule_chembl_id 的条目，不能返回空壳结果。"""
    monkeypatch.setattr(
        authority_client,
        "_http_get_json",
        lambda url, t: {"molecules": [{"pref_name": "X"}, dict(CHEMBL_NIVOLUMAB)]},
    )
    monkeypatch.setattr(authority_client, "_CHEMBL_MIN_INTERVAL", 0)
    mol = real_fetch_chembl("Nivolumab", 5.0)
    assert mol["molecule_chembl_id"] == "CHEMBL2108738"


def test_chembl_rate_limit_retries_once(monkeypatch):
    """429 后重试一次；两次都失败则向上抛，由调用方判为降级。"""
    state = {"n": 0}

    def _fake_get(url, _timeout):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)
        return {"molecules": [dict(CHEMBL_NIVOLUMAB)]}

    monkeypatch.setattr(authority_client, "_http_get_json", _fake_get)
    monkeypatch.setattr(authority_client, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(authority_client, "_CHEMBL_MIN_INTERVAL", 0)

    mol = real_fetch_chembl("Nivolumab", 5.0)
    assert state["n"] == 2
    assert mol["molecule_chembl_id"] == "CHEMBL2108738"


def test_chembl_failure_degrades_whole_resolution(monkeypatch):
    """ChEMBL 网络故障时整体标降级，而不是记成「查无此药」。"""
    monkeypatch.setattr(authority_client, "_fetch_cids", _no_cids)

    def _boom_chembl(_name, _timeout):
        raise urllib.error.URLError("EBI unreachable")

    monkeypatch.setattr(authority_client, "_fetch_chembl_molecule", _boom_chembl)

    res = resolve_identifier("Nivolumab")
    assert res["found"] is False
    assert res["degraded"] is True


@pytest.mark.parametrize(
    "name,chembl_id",
    [
        ("Nivolumab", "CHEMBL2108738"),
        ("Ipilimumab", "CHEMBL1789844"),
        ("Filgrastim", "CHEMBL1201567"),
        ("Pegfilgrastim", "CHEMBL1201568"),
        ("Etanercept", "CHEMBL1201572"),
    ],
)
def test_biologics_resolve_through_chembl(monkeypatch, name, chembl_id):
    """A0.8 的五个目标生物制品：PubChem 全部未命中，ChEMBL 全部应命中。

    ChEMBL ID 为 2026-10-27 实测落库值（data/authority/clinicaltrials/*.json）；
    此处 mock HTTP 层，不触网。
    ``std_id`` 前缀为 CHEMBL，便于落库后按标识符体系分组统计
    （B2 设计的 doc_authority_resolution.id_system）。
    """
    monkeypatch.setattr(authority_client, "_fetch_cids", _no_cids)
    monkeypatch.setattr(
        authority_client,
        "_fetch_chembl_molecule",
        lambda n, t: {"molecule_chembl_id": chembl_id, "pref_name": name.upper()},
    )

    res = resolve_identifier(name)
    assert res["found"] is True
    assert res["std_id"] == f"CHEMBL:{chembl_id}"
    assert res["std_id"].split(":", 1)[0] == "CHEMBL"
