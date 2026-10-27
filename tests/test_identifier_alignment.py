"""
T3 药名抽取与对齐的单元测试（阶段0 的 C1 / C2 两项修复）。

对应缺陷：
  C1  无药名时回退 title，把整条标题当药名送 PubChem（必然查不到，还白耗一次请求）
  C2  ClinicalTrials 的 "ASPIRIN; PLACEBO" 被整体查询 → 多药试验必然对齐失败

全部用 monkeypatch 替换 resolve_identifier，**不发起任何网络请求**。
不连接数据库、不写 data 目录。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core_engine import transform_pipeline
from core_engine.transform_pipeline import (
    _MAX_ALIGN_NAMES,
    _extract_drug_name,
    _extract_drug_names,
    apply_identifier_alignment,
)


def _doc(structured=None, extra=None, title=""):
    return {
        "uuid": "u-align-001",
        "title": title,
        "structured_data": structured or {},
        "extra": extra or {},
    }


class _Recorder:
    """记录 resolve_identifier 被调用的药名序列，返回可控结果。

    miss_names: 命中集合白名单——不在集合里的名字返回「确认未命中」，
                用于测试候选名回退（缺省时一律命中，保持旧用例语义）。
    degraded_names: 返回「降级」的名字，用于测试网络故障不换候选。
    """

    def __init__(self, result=None, raises=None, miss_names=None, degraded_names=None):
        self.calls = []
        self.result = result
        self.raises = raises
        self.miss_names = miss_names
        self.degraded_names = degraded_names or set()

    def __call__(self, name, timeout=None):
        self.calls.append(name)
        if self.raises is not None:
            raise self.raises
        if name in self.degraded_names:
            return {
                "found": False, "std_id": "", "source_id": "none",
                "raw_value": name, "resolved_value": name, "confidence": 0.0,
                "candidates": [], "degraded": True,
            }
        if self.miss_names is not None and name not in self.miss_names:
            return {
                "found": False, "std_id": "", "source_id": "none",
                "raw_value": name, "resolved_value": name, "confidence": 0.0,
                "candidates": [], "degraded": False,
            }
        if self.result is not None:
            return dict(self.result, raw_value=name)
        return {
            "found": True, "std_id": f"PUBCHEM:{abs(hash(name)) % 10000}",
            "source_id": "pubchem", "raw_value": name, "resolved_value": name,
            "confidence": 0.9, "candidates": [], "degraded": False,
        }


# ══════════════════════ 一、药名抽取（C1 / C2）══════════════════════
def test_no_drug_name_returns_empty_list():
    """没有药名字段时返回空列表，而不是抛错、也不是拿标题顶上。"""
    assert _extract_drug_names(_doc(title="国家药监局关于修订XX说明书的公告")) == []


def test_title_is_never_used_as_drug_name():
    """C1 核心：标题里有药名也不能当药名用（宁可漏，不可错）。"""
    doc = _doc(structured={}, extra={}, title="阿司匹林肠溶片说明书修订公告")
    assert _extract_drug_names(doc) == []
    assert _extract_drug_name(doc) == ""


def test_semicolon_separated_names_are_split():
    """C2 核心：ClinicalTrials 的 "; " 拼接必须拆成独立药名。"""
    doc = _doc(structured={"drug_name": "ASPIRIN; PLACEBO"})
    assert _extract_drug_names(doc) == ["ASPIRIN", "PLACEBO"]


def test_chinese_semicolon_separated_names_are_split():
    doc = _doc(extra={"drug_name": "阿司匹林；氯吡格雷；阿托伐他汀"})
    assert _extract_drug_names(doc) == ["阿司匹林", "氯吡格雷", "阿托伐他汀"]


def test_list_input_is_supported():
    """部分站点把多药名存成列表（json 数组），必须能接住。"""
    doc = _doc(structured={"drug_name": ["ASPIRIN", "PLACEBO"]})
    assert _extract_drug_names(doc) == ["ASPIRIN", "PLACEBO"]


def test_structured_data_takes_precedence_and_dedupes():
    doc = _doc(
        structured={"drug_name": "ASPIRIN; PLACEBO"},
        extra={"drug_name": "PLACEBO; HEPARIN"},
    )
    assert _extract_drug_names(doc) == ["ASPIRIN", "PLACEBO", "HEPARIN"]


def test_blank_and_placeholder_names_are_dropped():
    doc = _doc(structured={"drug_name": " ; ； ;ASPIRIN;;"})
    assert _extract_drug_names(doc) == ["ASPIRIN"]


def test_backward_compatible_single_name_returns_first():
    doc = _doc(structured={"drug_name": "ASPIRIN; PLACEBO"})
    assert _extract_drug_name(doc) == "ASPIRIN"


# ══════════════════════ 二、对齐执行 ══════════════════════
def test_alignment_runs_once_per_drug_name(monkeypatch):
    """两个药名 → 两条对齐结果，互不覆盖；安慰剂不送查但仍留痕。

    2026-10-27 变更：PLACEBO 属占位名，不再发起外部请求（真实数据里
    28 个待查名中有 3 个是 Placebo，白耗请求且必然 404），改为记录 skipped。
    """
    rec = _Recorder()
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    parsed = _doc(structured={"drug_name": "ASPIRIN; PLACEBO"})
    apply_identifier_alignment.fn(parsed)

    assert rec.calls == ["ASPIRIN"]
    assert set(parsed["authority_resolutions"]) == {"ASPIRIN", "PLACEBO"}
    assert parsed["authority_resolutions"]["ASPIRIN"]["found"] is True
    placebo = parsed["authority_resolutions"]["PLACEBO"]
    assert placebo["found"] is False
    assert placebo["skipped"] is True
    assert placebo["skip_reason"] == "placeholder"


def test_alignment_skips_network_when_no_drug_name(monkeypatch):
    """C1 的直接效益：没药名的文档不再发起任何外部请求。"""
    rec = _Recorder()
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    parsed = _doc(title="关于某某注射液获批上市的公告")
    apply_identifier_alignment.fn(parsed)

    assert rec.calls == []
    assert parsed["authority_resolutions"] == {}


def test_alignment_caps_number_of_requests(monkeypatch):
    """药名过多时只对齐前 N 个，避免单篇文档发起几十次外部请求。"""
    rec = _Recorder()
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    names = [f"DRUG{i}" for i in range(_MAX_ALIGN_NAMES + 4)]
    parsed = _doc(structured={"drug_name": "; ".join(names)})
    apply_identifier_alignment.fn(parsed)

    assert len(rec.calls) == _MAX_ALIGN_NAMES
    assert len(parsed["authority_resolutions"]) == _MAX_ALIGN_NAMES


def test_alignment_exception_is_degraded_not_raised(monkeypatch):
    """解析抛异常时必须降级记录，不能中断主流程。"""
    rec = _Recorder(raises=TimeoutError("boom"))
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    parsed = _doc(structured={"drug_name": "ASPIRIN"})
    apply_identifier_alignment.fn(parsed)  # 不应抛异常

    res = parsed["authority_resolutions"]["ASPIRIN"]
    assert res["found"] is False
    assert res["degraded"] is True
    assert res["raw_value"] == "ASPIRIN"


def test_alignment_records_lineage_step(monkeypatch):
    """对齐步必须在血缘里留痕，且 input 是原始药名；跳过的占位名单独记 op。"""
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", _Recorder())

    parsed = _doc(structured={"drug_name": "ASPIRIN; PLACEBO"})
    apply_identifier_alignment.fn(parsed)

    lineage = parsed.get("lineage") or {}
    aligned = [s for s in lineage.get("drug_name", []) if s.get("op") == "authority_align"]
    assert [s["input"] for s in aligned] == ["ASPIRIN"]
    # 每一步都要有 std_id 输出，不能是空转记录
    assert all(s["output"].startswith("PUBCHEM:") for s in aligned)

    skipped = [s for s in lineage.get("drug_name", []) if s.get("op") == "authority_align_skip"]
    assert [s["input"] for s in skipped] == ["PLACEBO"]


# ══════════════════════ 三、药名归一 + 候选回退（A0.5）══════════════════════
def test_alignment_falls_back_to_normalized_candidate(monkeypatch):
    """原值整段查不到时，用归一后的候选名重试，并记录命中的规则。

    真实场景：drug_name = 'Enbrel (Etanercept)'，整段查 PubChem 必然 404，
    括号里的通用名 'Etanercept' 才是可查的规范名。
    """
    rec = _Recorder(miss_names={"Etanercept"})
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    parsed = _doc(extra={"drug_name": "Enbrel (Etanercept)"})
    apply_identifier_alignment.fn(parsed)

    assert rec.calls == ["Enbrel (Etanercept)", "Etanercept"]
    res = parsed["authority_resolutions"]["Enbrel (Etanercept)"]
    assert res["found"] is True
    assert res["raw_value"] == "Enbrel (Etanercept)"   # 原始值不被覆盖
    assert res["candidate"] == "Etanercept"            # 实际送查的归一后名字
    assert res["rule"] == "inner_alias"
    assert res["candidates_tried"] == 2


def test_alignment_stops_at_first_hit(monkeypatch):
    """首个候选命中即停止，不再为同一条药名发更多请求。"""
    rec = _Recorder()
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    parsed = _doc(extra={"drug_name": "Nivolumab + Relatlimab"})
    apply_identifier_alignment.fn(parsed)

    assert rec.calls == ["Nivolumab + Relatlimab"]
    res = parsed["authority_resolutions"]["Nivolumab + Relatlimab"]
    assert res["rule"] == "as_is"
    assert res["candidates_tried"] == 1


def test_degraded_result_does_not_try_more_candidates(monkeypatch):
    """接口降级（限流/超时）时换名字也没用，必须立刻停，避免放大等待。

    'Nivolumab + Relatlimab' 有 2 个候选，若降级后继续回退会是 2 次请求。
    """
    rec = _Recorder(degraded_names={"Nivolumab + Relatlimab"})
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    parsed = _doc(extra={"drug_name": "Nivolumab + Relatlimab"})
    apply_identifier_alignment.fn(parsed)

    assert rec.calls == ["Nivolumab + Relatlimab"]
    res = parsed["authority_resolutions"]["Nivolumab + Relatlimab"]
    assert res["degraded"] is True
    assert res["found"] is False


def test_placeholder_never_reaches_network(monkeypatch):
    """安慰剂不送查：0 次请求，且结果标 skipped 而非 not_found。"""
    rec = _Recorder()
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    parsed = _doc(structured={"drug_name": "Placebo"})
    apply_identifier_alignment.fn(parsed)

    assert rec.calls == []
    res = parsed["authority_resolutions"]["Placebo"]
    assert res["skipped"] is True
    assert res["degraded"] is False


def test_total_requests_stay_bounded_by_candidates(monkeypatch):
    """候选回退会放大请求量，必须有上界：每次对齐 ≤ 4 次请求。"""
    from biz_rules.drug_name_normalizer import MAX_CANDIDATES_PER_NAME

    rec = _Recorder(miss_names=set())   # 全部未命中 → 每条都跑满候选
    monkeypatch.setattr(transform_pipeline, "resolve_identifier", rec)

    parsed = _doc(extra={
        "drug_name": ("Ifosfamide Injection 3g/60 mL, Single dose vial, Rx only; "
                      "Nivolumab + Relatlimab; Enbrel (Etanercept)")
    })
    apply_identifier_alignment.fn(parsed)

    assert len(rec.calls) <= 3 * MAX_CANDIDATES_PER_NAME
    assert len(parsed["authority_resolutions"]) == 3
