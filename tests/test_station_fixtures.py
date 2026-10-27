"""
按站点族的真实样本 fixture 测试（T1 补齐项）。

用 data/json_envelopes/ 下 15 站 × 10 条真实采集信封，替代原先的
`"正文内容 " * 50` 这类人造输入，验证三件事：

  1. 契约：每站真实样本都能转成质检输入，且质检输出结构合法；
  2. 基线：当前实测分数 / 权威分 / doc_type / 结构化字段与
     station_profiles.py 的声明一致（数据漂移立刻暴露）；
  3. 缺陷：仍未修复的 6 个缺陷用 xfail(strict=True) 固化——修好后会 XPASS 并
     让测试失败，强制更新声明表，避免「缺陷悄悄消失或悄悄新增」；
     已修复的 D1–D5 转入 FIXED_DEFECTS，由常规断言守护，防止白名单回退。

不连接数据库，不写 data 目录，只读。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from biz_rules.document_validator import validate_document
from biz_rules.source_mapper import calculate_confidence
from core_engine.quality_checker import check_quality

from conftest import ENVELOPE_DIR, _envelope_to_parsed
from station_profiles import (
    FIXED_DEFECTS,
    KNOWN_DEFECTS,
    MISSING_STATIONS,
    STATION_PROFILES,
    profile_of,
)

LEVELS = {"高", "中", "低", "隔离"}
DIMENSIONS = {"completeness", "accuracy", "authority", "consistency", "timeliness"}

ALL_SITES = [p["site"] for p in STATION_PROFILES]


def _converted(real_envelopes, site):
    return [_envelope_to_parsed(e) for e in real_envelopes(site)]


# ══════════════════════ 一、契约 ══════════════════════
@pytest.mark.parametrize("site", ALL_SITES)
def test_station_has_ten_real_samples(site, real_envelopes):
    """每站必须有 ≥10 条真实信封（少于 10 说明样本被删或目录名写错）。"""
    assert len(real_envelopes(site)) >= 10


@pytest.mark.parametrize("site", ALL_SITES)
def test_envelope_conversion_is_lossless(site, real_envelopes):
    """转换不丢正文：markdown 与 raw_content 完全相等，且非空。"""
    for env, parsed in zip(real_envelopes(site), _converted(real_envelopes, site)):
        assert parsed["markdown"] == env.get("raw_content", "")
        assert parsed["markdown"].strip(), f"{site}/{env.get('uuid')} 正文为空"
        assert parsed["uuid"] == env.get("uuid", "")


@pytest.mark.parametrize("site", ALL_SITES)
def test_quality_output_contract(site, real_envelopes):
    """质检输出结构对全部真实样本都成立：分数 0~1、五维齐全、等级合法。"""
    for parsed in _converted(real_envelopes, site):
        res = check_quality.fn(parsed)
        assert 0.0 <= res["quality_score"] <= 1.0
        assert res["quality_level"] in LEVELS
        assert set(res["quality_dimensions"]) == DIMENSIONS
        assert isinstance(res["quality_issues"], list)


# ══════════════════════ 二、基线声明与现实一致 ══════════════════════
@pytest.mark.parametrize("site", ALL_SITES)
def test_profile_authority_matches_reality(site, real_envelopes):
    """声明的权威分与实测一致（±0.01），防止白名单改动后无人更新声明表。"""
    env = real_envelopes(site)[0]
    url = (env.get("provenance") or {}).get("source_url", "")
    actual = calculate_confidence(source_url=url)["score"]
    assert abs(actual - profile_of(site)["authority_now"]) <= 0.01, (
        f"{site} 权威分已变为 {actual}，请更新 station_profiles"
    )


@pytest.mark.parametrize("site", ALL_SITES)
def test_profile_doc_type_and_language_match_reality(site, real_envelopes):
    env = real_envelopes(site)[0]
    profile = profile_of(site)
    assert (env.get("doc_type") or "") == profile["doc_type_now"]
    assert (env.get("language") or "") == profile["language"]


@pytest.mark.parametrize("site", ALL_SITES)
def test_profile_station_fields_are_present(site, real_envelopes):
    """声明的站点特有字段必须真实出现在首条样本里（子集判定，不要求穷举）。"""
    parsed = _envelope_to_parsed(real_envelopes(site)[0])
    actual = set(parsed["structured_data"])
    declared = set(profile_of(site)["station_fields"])
    assert declared <= actual, f"{site} 缺少声明字段: {declared - actual}"


@pytest.mark.parametrize("site", ALL_SITES)
def test_profile_title_empty_count_matches_reality(site, real_envelopes):
    empty = sum(1 for e in real_envelopes(site) if not (e.get("title") or "").strip())
    assert empty == profile_of(site)["title_empty"], (
        f"{site} 标题为空数已变为 {empty}，请更新 station_profiles"
    )


@pytest.mark.parametrize("site", ALL_SITES)
def test_score_baseline_snapshot(site, real_envelopes):
    """首条样本的总分与等级锁定为声明值（±0.002）。权重调整后须在此更新基线。"""
    res = check_quality.fn(_envelope_to_parsed(real_envelopes(site)[0]))
    profile = profile_of(site)
    assert abs(res["quality_score"] - profile["score_now"]) <= 0.002, (
        f"{site} 分数已变为 {res['quality_score']:.4f}，请更新 station_profiles"
    )
    assert res["quality_level"] == profile["level_now"]


# ══════════════════════ 三、族级差异（按站点细化的实质）══════════════════════
def test_family_c_registration_sites_carry_trial_fields(real_envelopes):
    """C 族两个登记库都必须真抽到核心字段；缺失的那部分由 D10/D11 的 xfail 固化。"""
    ct = [_envelope_to_parsed(e) for e in real_envelopes("clinicaltrials")]
    assert all("enrollment" in p["structured_data"] for p in ct), "clinicaltrials 应每条都有 enrollment"

    chictr = [_envelope_to_parsed(e) for e in real_envelopes("chictr")]
    assert all("regno" in p["structured_data"] for p in chictr), "chictr 应每条都有 regno"
    assert any("inclusion_criteria" in p["structured_data"] for p in chictr)


def test_family_d_chinese_notices_are_long_enough(real_envelopes):
    """D 族中文公告最短仍有 300+ 字 → 现有 100 字符阈值不会误判（反驳「中文阈值不公」）。"""
    for site in ("nmpa", "cde"):
        lengths = [len(e.get("raw_content") or "") for e in real_envelopes(site)]
        assert min(lengths) >= 300, f"{site} 出现短于 300 字的正文，中文阈值议题需重新评估"


def test_family_e_has_no_station_specific_fields(real_envelopes):
    """E 族（资讯/企业稿）实测无站点特有字段——这是数据现状，不是抽取失败。"""
    for site in ("fiercepharma", "nccn", "roche", "hkex"):
        parsed = _envelope_to_parsed(real_envelopes(site)[0])
        assert parsed["structured_data"] == {}, f"{site} 出现了预期外的结构化字段"


def test_family_a_has_no_crl_or_ema_samples():
    """A 族的 CRL / EMA 尚未采集，本地无样本 → 无法做真实 fixture。"""
    for item in MISSING_STATIONS:
        assert not (ENVELOPE_DIR / item["site"]).exists(), (
            f"{item['site']} 目录已出现，可移除 MISSING_STATIONS 登记并补 fixture"
        )


# ══════════════════════ 四、已知缺陷（修好即 XPASS → 失败）══════════════════════
def test_known_defects_registry_is_complete():
    """未修复缺陷锁定为 6 条，且每条站点都在声明表中——防止缺陷被悄悄增删。"""
    assert len(KNOWN_DEFECTS) == 6
    for d in KNOWN_DEFECTS:
        assert profile_of(d["site"])["site"] == d["site"]
        assert d["kind"] in {"scheme", "data"}


def test_fixed_defects_registry_is_complete():
    """已修复缺陷锁定为 5 条（D1–D5），每条保留 before/after 证据。"""
    assert len(FIXED_DEFECTS) == 5
    assert {d["id"] for d in FIXED_DEFECTS} == {"D1", "D2", "D3", "D4", "D5"}
    for d in FIXED_DEFECTS:
        assert d["after"] > d["before"], f"{d['id']} 的修复后数值必须高于修复前"


@pytest.mark.parametrize("site", [p["site"] for p in STATION_PROFILES if p["expect_official"]])
def test_official_sources_reach_full_authority(site, real_envelopes):
    """D1–D5 已修复的常规守护：所有官方/一手来源都必须拿到 ≥0.90 权威分。

    白名单一旦被回退，这条会立刻失败（原 xfail 版本只在缺陷存在时静默通过）。
    """
    env = real_envelopes(site)[0]
    url = (env.get("provenance") or {}).get("source_url", "")
    assert calculate_confidence(source_url=url)["score"] >= 0.90


@pytest.mark.xfail(strict=True, reason="D6：nccn 采集端写 doc_type='unknown'，不在 T5 枚举内")
def test_doc_type_passes_enum_validation(real_envelopes):
    env = real_envelopes("nccn")[0]
    report = validate_document({
        "uuid": env.get("uuid"),
        "title": env.get("title"),
        "raw_content": env.get("raw_content"),
        "publish_date": env.get("publish_date"),
        "category": env.get("category"),
        "language": env.get("language"),
        "status": env.get("status"),
        "summary": env.get("summary"),
        "doc_type": env.get("doc_type"),
    })
    assert not any(v["rule"] == "enum" and v["field"] == "doc_type" for v in report.violations)


@pytest.mark.xfail(strict=True, reason="D7–D9：标题抽取缺失，属采集数据缺陷，需重采")
@pytest.mark.parametrize("site", ["roche", "fda_api_drug_label", "fiercepharma"])
def test_station_titles_are_not_empty(site, real_envelopes):
    empty = [e.get("uuid") for e in real_envelopes(site) if not (e.get("title") or "").strip()]
    assert empty == []


def test_clinicaltrials_has_registration_id(real_envelopes):
    """C 族最关键的身份字段是登记号，10/10 必须都能拿到合法 NCT 号。

    历史：extra_fields 配置晚于数据生成，存量 extra 全是 Pydantic 默认值；
    A0.7 已从 raw_content 的原始 protocolSection 离线重建为 10/10。
    """
    envs = real_envelopes("clinicaltrials")
    assert envs
    for env in envs:
        nct = (env.get("extra") or {}).get("nct_id", "")
        assert nct.startswith("NCT") and len(nct) == 11, f"{env.get('uuid')} → {nct!r}"
        # 信封经 _envelope_to_parsed 后，非空 extra 会并入 structured_data
        assert "nct_id" in _envelope_to_parsed(env)["structured_data"]


@pytest.mark.xfail(strict=True, reason="D11：chictr 的 sample_size / recruitment_status 为空，样本量永远未知")
def test_chictr_has_sample_size(real_envelopes):
    for env in real_envelopes("chictr"):
        assert "sample_size" in _envelope_to_parsed(env)["structured_data"]
