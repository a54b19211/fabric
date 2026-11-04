"""v3 业务验收契约（contracts.acceptance）的测试。

为什么要有这份测试：v3 第一部分的验收案例、第四部分每条链路的正例/更正/异常/判断E、6.4 的三种使用视角，
如果只写在文档里，实现者很容易用“报告写得像”蒙混过关。这里把它们固定成 JSON 夹具，并用校验器保证：
- 每个验收案例恰好出现一次，不漏、不重复；
- 每个案例都有反例和可机检的断言，禁止的捷径写进 must_not；
- 证据类型和来源编号只能用约定的词表，缺口必须如实列出；
- 夹具里不出现编造的真实世界编号（NCT、PMID、DOI 等），材料没找到就标 to_be_sourced。
审阅 8 条裁定各有对应测试：来源三栏状态与同上游、更正看判断结果、E 只定义一次、
E 的触发与结果覆盖、材料真实性与故障注入分开、目标与现状分开、内参保留不确定性。
纯本地测试，不联网、不连库。
"""

import copy
import json
import re
import shutil
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts import acceptance as acc  # noqa: E402

FIXTURE_DIR = ROOT / "contracts" / "acceptance"
FILES = ["A.json", "B.json", "C.json", "D.json", "E.json", "views.json"]


@pytest.fixture(scope="module")
def cases():
    return acc.load_all(FIXTURE_DIR)


def raw_cases(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def good_case():
    """取 A.json 的第一个案例作为合法样本，每个负例测试只改一处，确保拒绝原因唯一。"""
    return copy.deepcopy(raw_cases("A.json")[0])


def kinds(case):
    return {c.kind for c in case.checks}


# ---------- 词表与常量 ----------

def test_evidence_vocabulary_matches_v3_1_4():
    # v3 1.4 的 12 种证据类型，顺序和原文一致；slug 是英文标识
    assert list(acc.EVIDENCE_TYPES.items()) == [
        ("registry", "登记"), ("regulatory_decision", "监管决定"), ("primary_research", "原始研究"),
        ("preprint", "预印本"), ("conference", "会议"), ("company_disclosure", "公司披露"),
        ("financial", "财务"), ("policy", "政策"), ("official_notice", "官方通报"),
        ("media", "媒体"), ("social", "社媒"), ("patent", "专利"),
    ]


GIVEN_SOURCES = [
    "clinicaltrials", "europe_pmc", "pubmed", "who_don", "sec_companyfacts", "fda_crl",
    "nmpa", "cde", "chictr", "cde_ctr", "cninfo", "hkex", "biorxiv", "drugs_fda",
]


def status3(sid):
    s = acc.KNOWN_SOURCES[sid]
    return (s.old_implementation_exists, s.new_framework_connected, s.live_collecting_or_indexed)


def test_known_source_ids_given_and_introduced():
    given = set(GIVEN_SOURCES)
    introduced = {sid for sid, s in acc.KNOWN_SOURCES.items() if s.introduced_here}
    # 本模块新增的编号必须显式标出来，且不和给定编号重叠
    assert introduced and not (introduced & given)
    assert set(acc.KNOWN_SOURCES) == given | introduced
    for sid in acc.KNOWN_SOURCES:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", sid), sid


def test_source_status_is_three_separate_fields():
    # 裁定 2：旧实现 / 新框架接通 / 生产在采或已索引 分三栏写，不再用一个 coverage 混写
    assert "coverage" not in acc.SourceInfo.model_fields
    for f in ("old_implementation_exists", "new_framework_connected", "live_collecting_or_indexed"):
        assert f in acc.SourceInfo.model_fields, f


def test_source_status_facts_from_review():
    # 审阅给出的事实；不知道的写 unknown，不拿 v3 或旧目录当唯一真相
    assert status3("hkex") == ("yes", "declared_not_connected", "no")
    assert status3("sec_companyfacts")[1:] == ("active", "unknown")
    for sid in ("clinicaltrials", "europe_pmc", "who_don"):
        assert status3(sid)[1:] == ("active", "collecting_and_indexed"), sid
    for sid in ("nmpa", "cde", "chictr"):
        assert status3(sid) == ("yes", "legacy_plugin_unverified", "old_stock_only"), sid
    assert status3("cde_ctr") == ("no", "no", "no")
    # 审阅没给事实的给定来源，不得写成在采
    for sid in ("pubmed", "fda_crl"):
        assert acc.KNOWN_SOURCES[sid].live_collecting_or_indexed == "unknown", sid
    assert "需要证据" in acc.KNOWN_SOURCES["sec_companyfacts"].note


def test_source_availability_derived_from_status():
    av = {sid: s.availability for sid, s in acc.KNOWN_SOURCES.items()}
    assert av["clinicaltrials"] == "has_material"
    assert av["nmpa"] == "has_material"  # 旧库存量是状态，不是来源定义
    assert av["openfda"] == "has_material"
    assert av["sec_companyfacts"] == "unknown"
    assert av["hkex"] == "gap" and av["cde_ctr"] == "gap"


def test_rejects_inconsistent_source_status():
    # 未实测的旧插件、只声明未接通的插件，都不能写成生产在采
    for new in ("legacy_plugin_unverified", "declared_not_connected", "no"):
        with pytest.raises(ValueError, match="collecting_and_indexed"):
            acc.SourceInfo(name="x", old_implementation_exists="yes", new_framework_connected=new,
                           live_collecting_or_indexed="collecting_and_indexed")
    with pytest.raises(ValueError, match="old_implementation_exists"):
        acc.SourceInfo(name="x", old_implementation_exists="no", new_framework_connected="no",
                       live_collecting_or_indexed="old_stock_only")


def test_openfda_states_real_endpoints_and_stock():
    # 裁定 1：openfda 写清真实端点/数据类型和旧库存量；“旧库存量”只是状态
    o = acc.KNOWN_SOURCES["openfda"]
    text = " ".join(o.datasets)
    assert "drug/label" in text and "enforcement" in text
    assert o.old_stock_docs == {"fda_api_drug_label": 258915, "fda_api_enforcement": 17684}
    assert "旧库存量" not in o.name
    crl = acc.KNOWN_SOURCES["fda_crl"]
    assert any("transparency/crl" in d for d in crl.datasets)
    # 两者按覆盖的数据集分开表达，数据集不重叠
    assert not set(o.datasets) & set(crl.datasets)


def test_openfda_and_crl_share_upstream_never_two_confirmations():
    o, crl = acc.KNOWN_SOURCES["openfda"], acc.KNOWN_SOURCES["fda_crl"]
    assert o.upstream and o.upstream == crl.upstream
    assert acc.count_independent_upstreams(["openfda", "fda_crl"]) == 1
    assert acc.count_independent_upstreams(["openfda", "clinicaltrials"]) == 2
    assert acc.count_independent_upstreams(["fda_crl", "fda_crl"]) == 1


def test_rejects_shared_upstream_without_check(cases):
    both = [c for c in cases
            if {"openfda", "fda_crl"} <= set(c.sources_available + c.sources_status_unknown + c.sources_gap)]
    assert both, "至少一个案例同时用到 openfda 与 fda_crl，用来验同上游不算两份证实"
    for c in both:
        assert "same_upstream_not_independent" in kinds(c), c.id
    raw = [r for r in raw_cases("A.json") if r["id"] == both[0].id][0]
    raw["checks"] = [k for k in raw["checks"] if k["kind"] != "same_upstream_not_independent"]
    with pytest.raises(ValueError, match="same_upstream_not_independent"):
        acc.validate_case(raw)


def test_rejects_unknown_status_source_listed_as_available():
    c = good_case()
    c["sources_available"] = c["sources_available"] + ["sec_companyfacts"]
    with pytest.raises(ValueError, match="sec_companyfacts"):
        acc.validate_case(c)


def test_unknown_status_sources_need_verification_check(cases):
    for c in cases:
        if c.sources_status_unknown:
            assert "source_status_verified" in kinds(c), c.id
    for c in cases:
        assert "sec_companyfacts" not in c.sources_available, c.id


def test_required_design_refs_count():
    # 1.2 五个判断各 2 个验收案例 + 第四部分 4 条链路各 4 项 + 6.4 三种视角
    assert len(acc.REQUIRED_DESIGN_REFS) == 10 + 16 + 3
    assert len(set(acc.REQUIRED_DESIGN_REFS)) == len(acc.REQUIRED_DESIGN_REFS)


# ---------- 夹具整体 ----------

def test_load_all_loads_every_file(cases):
    by_j = Counter(c.judgment for c in cases)
    assert by_j["A"] >= 2 and by_j["B"] >= 2 and by_j["C"] >= 2
    assert by_j["D"] >= 2 and by_j["E"] >= 2 and by_j["view"] >= 3
    assert sum(len(raw_cases(f)) for f in FILES) == len(cases)


@pytest.mark.parametrize("name,judgment", [
    ("A.json", "A"), ("B.json", "B"), ("C.json", "C"), ("D.json", "D"), ("E.json", "E"),
    ("views.json", "view"),
])
def test_file_holds_only_its_judgment(name, judgment):
    for c in raw_cases(name):
        case = acc.validate_case(c)
        assert case.judgment == judgment
        prefix = "V" if judgment == "view" else judgment
        assert case.id.startswith(prefix + "-")


def test_each_design_ref_appears_exactly_once(cases):
    counts = Counter(c.design_ref for c in cases)
    assert set(counts) == set(acc.REQUIRED_DESIGN_REFS)
    assert all(n == 1 for n in counts.values()), counts


def test_ids_unique(cases):
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))


def test_every_case_says_the_five_report_items(cases):
    # v3 1.3：每份报告必须说清的五件事
    for c in cases:
        for item in acc.REPORT_MUST_SAY:
            assert item in c.must_say, (c.id, item)


@pytest.mark.parametrize("judgment", ["A", "B", "C", "D", "E", "view"])
def test_forbidden_shortcuts_present(cases, judgment):
    for c in (c for c in cases if c.judgment == judgment):
        for item in acc.REQUIRED_MUST_NOT[judgment]:
            assert item in c.must_not, (c.id, item)


def test_judgment_specific_must_say(cases):
    for c in cases:
        for item in acc.JUDGMENT_MUST_SAY[c.judgment]:
            assert item in c.must_say, (c.id, item)


def test_views_use_the_five_6_4_criteria(cases):
    views = [c for c in cases if c.judgment == "view"]
    assert {c.design_ref for c in views} == {
        "v3 6.4 使用视角1 国家层", "v3 6.4 使用视角2 省级", "v3 6.4 使用视角3 高校/科研机构",
    }
    for c in views:
        assert set(acc.VIEW_EVALUATION_CHECKS) <= kinds(c), c.id
        for item in acc.BRIEFING_STRUCTURE:
            assert item in c.must_say, (c.id, item)
        # 内部台账是合成的：只验证流程，不能宣布已覆盖真实政府场景
        assert c.synthetic_components
        assert "no_real_coverage_claim" in kinds(c)


def test_views_are_evaluated_on_substance_not_style():
    # 6.4：评价不看文风像不像内参，五个标准都必须有对应的断言种类
    assert set(acc.VIEW_EVALUATION_CHECKS) == {
        "answers_pending_question", "key_evidence_not_missed", "counter_evidence_changes_judgment",
        "gap_stated", "updatable_with_new_evidence",
    }
    assert not any("文风" in k or "style" in k for k in acc.CHECK_KINDS)


def test_work_modes_cover_a_to_d(cases):
    # 1.1：三种工作方式都要覆盖五类判断；E 本身就是更新机制，只能是 judgment_update
    for j in "ABCD":
        modes = {c.work_mode for c in cases if c.judgment == j}
        assert modes == {"discovery", "investigation", "judgment_update"}, (j, modes)
    assert {c.work_mode for c in cases if c.judgment == "E"} == {"judgment_update"}


def test_chain_cases_cover_four_kinds_per_chain(cases):
    for chain in "ABCD":
        got = {c.case_kind for c in cases if c.chain == chain and c.case_kind.startswith("chain_")}
        assert got == {"chain_positive", "chain_correction", "chain_anomaly", "chain_judgment_e"}, chain
    # 链路上的判断 E 归在 E.json 里
    for c in cases:
        if c.case_kind == "chain_judgment_e":
            assert c.judgment == "E"


def test_graph_and_mechanism_cases_exist(cases):
    # 第四部分：至少一个确实需要查图谱的案例，一个确实需要机制评价的案例
    assert any("uses_graph_lookup" in kinds(c) for c in cases)
    assert any("uses_mech_evaluation" in kinds(c) for c in cases)


def test_correction_cases_recompute_only_affected_and_keep_old(cases):
    corr = [c for c in cases if c.case_kind == "chain_correction"]
    assert len(corr) == 4
    for c in corr:
        assert {"recompute_only_affected", "keeps_old_judgment"} <= kinds(c), c.id
    for c in cases:
        if c.judgment == "E":
            assert {"keeps_old_judgment", "revision_reason_stated"} <= kinds(c), c.id


CORRECTION_OUTCOME_CHECKS = {"judgment_maintained_or_revised", "old_citations_kept",
                             "affected_computations_updated"}


def test_correction_cases_check_outcome_not_just_file_change(cases):
    # 裁定 3：更正验收要看判断维持还是修订、旧引用保留、受影响计算已更新，不是只看“文件变了”
    for c in cases:
        if c.case_kind == "chain_correction":
            assert CORRECTION_OUTCOME_CHECKS <= kinds(c), c.id
            assert "只报告“文件变了”而不判断维持还是修订" in c.must_not, c.id


def test_correction_trigger_matches_chain(cases):
    # 链路 B 更正是勘误改数值；链路 C 更正是公告/附件新版本改了测量值或口径
    by_chain = {c.chain: c for c in cases if c.case_kind == "chain_correction"}
    assert by_chain["B"].correction_trigger == "erratum_number_change"
    assert by_chain["C"].correction_trigger == "filing_measurement_or_caliber_change"
    assert by_chain["A"].correction_trigger == "registry_field_correction"
    assert by_chain["D"].correction_trigger == "notice_count_or_definition_change"
    assert "口径" in by_chain["C"].question and "数值" in by_chain["B"].question
    # E-01 撤稿留在 E
    assert any(c.id == "E-01" and "撤稿" in c.question for c in cases)


def test_rejects_correction_with_wrong_trigger_or_missing_outcome_check():
    c = copy.deepcopy([r for r in raw_cases("C.json") if r["case_kind"] == "chain_correction"][0])
    bad = copy.deepcopy(c)
    bad["correction_trigger"] = "erratum_number_change"
    with pytest.raises(ValueError, match="correction_trigger"):
        acc.validate_case(bad)
    bad = copy.deepcopy(c)
    bad["correction_trigger"] = None
    with pytest.raises(ValueError, match="correction_trigger"):
        acc.validate_case(bad)
    for kind in CORRECTION_OUTCOME_CHECKS:
        bad = copy.deepcopy(c)
        bad["checks"] = [k for k in bad["checks"] if k["kind"] != kind]
        with pytest.raises(ValueError, match=kind):
            acc.validate_case(bad)


# ---------- 裁定 4：E 案例只在 E.json，其他链路按编号引用 ----------

def test_chain_e_cases_referenced_from_their_chain_file(cases):
    by_id = {c.id: c for c in cases}
    for e in (c for c in cases if c.case_kind == "chain_judgment_e"):
        refs = [c for c in cases if e.id in c.related_cases and c.judgment == e.chain]
        assert refs, (e.id, "所在链路的文件里要有案例用 related_cases 引用它")
    for c in cases:
        for rid in c.related_cases:
            assert rid in by_id and rid != c.id, (c.id, rid)


def test_score_units_count_each_case_once(cases):
    units = acc.score_units(cases)
    assert len(units) == len(set(units)) == len(cases)
    referenced = {r for c in cases for r in c.related_cases}
    assert referenced and all(units.count(r) == 1 for r in referenced)


def test_validate_case_set_rejects_dangling_or_self_reference():
    all_cases = [acc.validate_case(r) for f in FILES for r in raw_cases(f)]
    acc.validate_case_set(all_cases)
    a = copy.deepcopy(raw_cases("A.json"))
    a[0]["related_cases"] = ["E-99"]
    bad = [acc.validate_case(r) for r in a] + [c for c in all_cases if c.judgment != "A"]
    with pytest.raises(ValueError, match="E-99"):
        acc.validate_case_set(bad)
    a = copy.deepcopy(raw_cases("A.json"))
    a[0]["related_cases"] = [a[0]["id"]]
    bad = [acc.validate_case(r) for r in a] + [c for c in all_cases if c.judgment != "A"]
    with pytest.raises(ValueError, match=a[0]["id"]):
        acc.validate_case_set(bad)


def test_validate_case_set_rejects_unreferenced_chain_e():
    all_cases = [acc.validate_case(r) for f in FILES for r in raw_cases(f)]
    d = copy.deepcopy(raw_cases("D.json"))
    for r in d:
        r["related_cases"] = []
    bad = [c for c in all_cases if c.judgment != "D"] + [acc.validate_case(r) for r in d]
    with pytest.raises(ValueError, match="E-06"):
        acc.validate_case_set(bad)


def test_load_all_rejects_same_id_in_two_files(tmp_path):
    # 同一案例只能在一个文件里定义；放进第二个文件就是重复计分
    d = tmp_path / "acceptance"
    shutil.copytree(FIXTURE_DIR, d)
    e_cases = json.loads((d / "E.json").read_text(encoding="utf-8"))
    a_cases = json.loads((d / "A.json").read_text(encoding="utf-8"))
    a_cases.append(copy.deepcopy(e_cases[2]))
    (d / "A.json").write_text(json.dumps(a_cases, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=r"E-03.*A\.json.*E\.json"):
        acc.load_all(d)


# ---------- 裁定 5：判断 E 的触发方式与结果 ----------

def test_e_cases_cover_three_triggers_and_both_outcomes(cases):
    e = [c for c in cases if c.judgment == "E"]
    assert {c.trigger for c in e} == {"new_material_auto", "user_follow_up", "historical_replay"}
    outcomes = {c.expected_outcome for c in e}
    assert {"maintain", "revise"} <= outcomes
    for c in e:
        assert c.work_mode == "judgment_update"
        assert "judgment_maintained_or_revised" in kinds(c), c.id


@pytest.mark.parametrize("trigger,need", [
    ("new_material_auto", "auto_triggered_by_new_version"),
    ("user_follow_up", "follow_up_supported"),
    ("historical_replay", "visibility_cutoff_respected"),
])
def test_trigger_requires_its_check(cases, trigger, need):
    for c in cases:
        if c.trigger == trigger:
            assert need in kinds(c), c.id
    raw = [r for r in raw_cases("E.json") if r["trigger"] == trigger][0]
    raw["checks"] = [k for k in raw["checks"] if k["kind"] != need]
    with pytest.raises(ValueError, match=need):
        acc.validate_case(raw)


@pytest.mark.parametrize("field", ["trigger", "expected_outcome"])
def test_rejects_e_case_without_trigger_or_outcome(field):
    c = copy.deepcopy(raw_cases("E.json")[0])
    c[field] = None
    with pytest.raises(ValueError, match=field):
        acc.validate_case(c)


@pytest.mark.parametrize("field,value,missing", [
    ("trigger", "new_material_auto", "user_follow_up"),
    ("expected_outcome", "revise", "maintain"),
])
def test_validate_case_set_rejects_missing_e_coverage(field, value, missing):
    all_raw = [r for f in FILES for r in raw_cases(f)]
    for r in all_raw:
        if r["judgment"] == "E":
            r[field] = value
            # 改 trigger 后单案例断言要补齐；这里只验集合层面的覆盖
            r["checks"] = r["checks"] + [{"kind": "auto_triggered_by_new_version", "detail": "测试补齐"}]
    with pytest.raises(ValueError, match=missing):
        acc.validate_case_set([acc.validate_case(r) for r in all_raw])


# ---------- 裁定 6：材料真实性与故障注入分开标 ----------

def test_data_truth_and_fault_injected_labelled(cases):
    for c in cases:
        assert c.data_truth in ("real_fixed_version", "synthetic", "to_be_sourced"), c.id
        assert isinstance(c.fault_injected, bool), c.id
        if c.case_kind == "chain_anomaly":
            assert c.fault_injected, c.id
        if c.fault_injected:
            assert "fault_not_reported_as_incident" in kinds(c), c.id
            assert "把注入的隔离故障写成生产事故" in c.must_not, c.id
    # 优先在真实固定版本上做隔离故障注入
    anomalies = [c for c in cases if c.case_kind == "chain_anomaly"]
    assert sum(c.data_basis == "real_fixed_version" for c in anomalies) >= 3


def test_rejects_anomaly_without_fault_injected():
    c = copy.deepcopy([r for r in raw_cases("A.json") if r["case_kind"] == "chain_anomaly"][0])
    c["fault_injected"] = False
    with pytest.raises(ValueError, match="fault_injected"):
        acc.validate_case(c)


def test_rejects_fault_case_without_incident_guard():
    c = copy.deepcopy([r for r in raw_cases("A.json") if r["case_kind"] == "chain_anomaly"][0])
    c["checks"] = [k for k in c["checks"] if k["kind"] != "fault_not_reported_as_incident"]
    with pytest.raises(ValueError, match="fault_not_reported_as_incident"):
        acc.validate_case(c)


def test_rejects_synthetic_truth_under_real_target():
    c = good_case()
    assert c["data_basis"].startswith("real_")
    c["data_truth"] = "synthetic"
    with pytest.raises(ValueError, match="data_truth"):
        acc.validate_case(c)


def test_synthetic_never_counts_as_real_coverage(cases):
    # 当前没有任何案例跑过，所以真实覆盖为空；合成材料即使“通过”也不算
    assert acc.real_coverage_cases(cases) == []
    c = copy.deepcopy(raw_cases("D.json")[-1])
    c["status"] = "passed"
    c["data_truth"] = "synthetic"
    c["data_basis"] = "synthetic_flow_only"
    assert acc.real_coverage_cases([acc.validate_case(c)]) == []
    r = good_case()
    r["status"] = "passed"
    r["data_truth"] = "real_fixed_version"
    assert [x.id for x in acc.real_coverage_cases([acc.validate_case(r)])] == [r["id"]]


# ---------- 裁定 7：目标与现状分开；武汉回放 ----------

def test_rejects_executable_status_without_material():
    for st in ("ready", "passed", "failed"):
        c = good_case()
        assert c["data_truth"] == "to_be_sourced"
        c["status"] = st
        with pytest.raises(ValueError, match="status"):
            acc.validate_case(c)


def test_no_case_claims_material_or_pass_yet(cases):
    for c in cases:
        assert c.material_note.strip(), c.id
        assert c.status == "to_be_sourced", c.id
        assert not c.executable, c.id


def test_wuhan_replay_target_vs_status(cases):
    d = [c for c in cases if c.design_ref == "v3 1.2 判断D 验收案例1"]
    assert len(d) == 1
    w = d[0]
    assert "武汉" in w.question
    assert w.data_basis == "real_public_replay"  # 目标
    assert w.data_truth == "to_be_sourced" and w.status == "to_be_sourced"  # 现状
    assert not w.executable
    assert {"visibility_cutoff_respected", "visibility_proof_bound", "judged_correctly_first"} <= kinds(w)
    assert "把最早报警当作成功指标" in w.must_not
    assert "13.5" in " ".join(k.detail for k in w.checks if k.kind == "judged_correctly_first")


def test_rejects_public_replay_without_proof_or_first_correct():
    c = copy.deepcopy(raw_cases("D.json")[0])
    for kind in ("visibility_proof_bound", "judged_correctly_first"):
        bad = copy.deepcopy(c)
        bad["checks"] = [k for k in bad["checks"] if k["kind"] != kind]
        with pytest.raises(ValueError, match=kind):
            acc.validate_case(bad)
    bad = copy.deepcopy(c)
    bad["must_not"] = [x for x in bad["must_not"] if x != "把最早报警当作成功指标"]
    with pytest.raises(ValueError, match="最早报警"):
        acc.validate_case(bad)


# ---------- 裁定 8：内参 ----------

def test_views_keep_1_3_and_6_4_items(cases):
    views = [c for c in cases if c.judgment == "view"]
    for c in views:
        for item in acc.REPORT_MUST_SAY + acc.VIEW_MUST_SAY:
            assert item in c.must_say, (c.id, item)
        assert "uncertainty_not_upgraded" in kinds(c), c.id
        assert "面向更高层级时把不确定性写成确定结论" in c.must_not, c.id
    assert set(acc.VIEW_MUST_SAY) == {"决策对象", "口径", "内部底册缺口", "行动条件", "不确定性"}


def test_rejects_view_without_uncertainty_guard():
    c = copy.deepcopy(raw_cases("views.json")[0])
    bad = copy.deepcopy(c)
    bad["checks"] = [k for k in bad["checks"] if k["kind"] != "uncertainty_not_upgraded"]
    with pytest.raises(ValueError, match="uncertainty_not_upgraded"):
        acc.validate_case(bad)
    bad = copy.deepcopy(c)
    bad["must_say"] = [x for x in bad["must_say"] if x != "不确定性"]
    with pytest.raises(ValueError, match="不确定性"):
        acc.validate_case(bad)


def test_anomaly_cases_cover_the_four_failure_modes(cases):
    anomalies = [c for c in cases if c.case_kind == "chain_anomaly"]
    assert len(anomalies) == 4
    need = {"no_duplicate_on_restart", "resumes_after_partial_failure", "no_derived_feedback_loop",
            "identifier_not_double_counted"}
    for c in anomalies:
        assert need <= kinds(c), c.id


def test_specific_v3_shortcuts_are_checked(cases):
    by_ref = {c.design_ref: c for c in cases}
    assert "no_efficacy_from_status" in kinds(by_ref["v3 1.2 判断A 验收案例1"])
    assert "no_hype_overestimate" in kinds(by_ref["v3 1.2 判断A 验收案例2"])
    assert "contract_not_revenue" in kinds(by_ref["v3 1.2 判断C 验收案例2"])
    assert "recompute_only_affected" in kinds(by_ref["v3 1.2 判断C 验收案例1"])
    assert "same_origin_not_independent" in kinds(by_ref["v3 1.2 判断D 验收案例2"])
    assert "permission_enforced" in kinds(by_ref["v3 1.2 判断E 验收案例2"])


def test_synthetic_basis_never_claims_real_coverage(cases):
    for c in cases:
        if c.data_basis == "synthetic_flow_only" or c.synthetic_components:
            assert "no_real_coverage_claim" in kinds(c), c.id


def test_no_invented_real_world_identifiers():
    # 夹具里不允许出现看起来像真实编号的字符串；真实材料到位前一律标 to_be_sourced
    patterns = [
        r"NCT\d{8}", r"CTR\d{8}", r"ChiCTR[-\d]", r"PMID[:：\s]*\d", r"PMC\d{4,}", r"\b10\.\d{4,9}/",
        r"\b\d{6}\.(SH|SZ|BJ)\b", r"\b\d{4,5}\.HK\b", r"\bCIK\s*\d",
    ]
    for name in FILES:
        text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
        for p in patterns:
            assert not re.search(p, text), (name, p)


# ---------- 校验器必须拒绝的情况 ----------

def test_validate_case_accepts_good_sample():
    case = acc.validate_case(good_case())
    assert isinstance(case, acc.AcceptanceCase)
    assert acc.validate_case(case) is case


def test_rejects_unknown_evidence_slug():
    c = good_case()
    c["required_evidence"] = c["required_evidence"] + ["blog"]
    with pytest.raises(ValueError, match="blog"):
        acc.validate_case(c)


def test_rejects_chinese_evidence_label_instead_of_slug():
    c = good_case()
    c["required_evidence"] = ["登记"]
    with pytest.raises(ValueError):
        acc.validate_case(c)


@pytest.mark.parametrize("field", ["sources_available", "sources_gap"])
def test_rejects_unknown_source_id(field):
    c = good_case()
    c[field] = c[field] + ["made_up_source"]
    with pytest.raises(ValueError, match="made_up_source"):
        acc.validate_case(c)


def test_rejects_gap_source_listed_as_available():
    c = good_case()
    c["sources_gap"] = [s for s in c["sources_gap"] if s != "cde_ctr"]
    c["sources_available"] = c["sources_available"] + ["cde_ctr"]
    with pytest.raises(ValueError, match="cde_ctr"):
        acc.validate_case(c)


def test_rejects_available_source_listed_as_gap():
    c = good_case()
    c["sources_available"] = [s for s in c["sources_available"] if s != "clinicaltrials"]
    c["sources_gap"] = c["sources_gap"] + ["clinicaltrials"]
    with pytest.raises(ValueError, match="clinicaltrials"):
        acc.validate_case(c)


def test_rejects_missing_counterexample():
    c = good_case()
    del c["counterexample"]
    with pytest.raises(ValueError, match="counterexample"):
        acc.validate_case(c)


def test_rejects_null_counterexample():
    c = good_case()
    c["counterexample"] = None
    with pytest.raises(ValueError, match="counterexample"):
        acc.validate_case(c)


def test_rejects_counterexample_without_must_not_conclude():
    c = good_case()
    c["counterexample"]["must_not_conclude"] = []
    with pytest.raises(ValueError, match="must_not_conclude"):
        acc.validate_case(c)


def test_rejects_empty_checks():
    c = good_case()
    c["checks"] = []
    with pytest.raises(ValueError, match="checks"):
        acc.validate_case(c)


def test_rejects_unknown_check_kind():
    c = good_case()
    c["checks"] = c["checks"] + [{"kind": "looks_like_briefing", "detail": "文风像内参"}]
    with pytest.raises(ValueError, match="looks_like_briefing"):
        acc.validate_case(c)


def test_rejects_unknown_field():
    c = good_case()
    c["confidence_pct"] = 87
    with pytest.raises(ValueError):
        acc.validate_case(c)


def test_rejects_id_prefix_mismatch():
    c = good_case()
    c["id"] = "B-99"
    with pytest.raises(ValueError, match="B-99"):
        acc.validate_case(c)


def test_rejects_unknown_design_ref():
    c = good_case()
    c["design_ref"] = "v3 1.2 判断A 验收案例9"
    with pytest.raises(ValueError, match="design_ref"):
        acc.validate_case(c)


def test_rejects_missing_report_items():
    c = good_case()
    c["must_say"] = [x for x in c["must_say"] if x != "局限"]
    with pytest.raises(ValueError, match="局限"):
        acc.validate_case(c)


def test_rejects_missing_forbidden_shortcut():
    c = good_case()
    c["must_not"] = []
    with pytest.raises(ValueError):
        acc.validate_case(c)


def test_rejects_synthetic_without_no_real_coverage_claim():
    c = good_case()
    c["data_basis"] = "synthetic_flow_only"
    c["checks"] = [k for k in c["checks"] if k["kind"] != "no_real_coverage_claim"]
    with pytest.raises(ValueError, match="no_real_coverage_claim"):
        acc.validate_case(c)


def test_rejects_missing_fixed_version_citation():
    # 第四部分正例：报告必须能回指原文，所以每个案例都要断言 cites_fixed_version
    c = good_case()
    c["checks"] = [k for k in c["checks"] if k["kind"] != "cites_fixed_version"]
    with pytest.raises(ValueError, match="cites_fixed_version"):
        acc.validate_case(c)


def test_rejects_gaps_without_gap_stated():
    c = good_case()
    assert c["sources_gap"]
    c["checks"] = [k for k in c["checks"] if k["kind"] != "gap_stated"]
    with pytest.raises(ValueError, match="gap_stated"):
        acc.validate_case(c)


def test_rejects_design_ref_on_wrong_case_kind():
    c = good_case()
    c["case_kind"] = "chain_positive"
    c["chain"] = "A"
    with pytest.raises(ValueError, match="design_ref"):
        acc.validate_case(c)


def test_rejects_judgment_e_outside_judgment_update():
    c = copy.deepcopy(raw_cases("E.json")[0])
    c["work_mode"] = "investigation"
    with pytest.raises(ValueError, match="judgment_update"):
        acc.validate_case(c)


def test_validate_cases_rejects_duplicate_ids():
    a = good_case()
    b = copy.deepcopy(a)
    with pytest.raises(ValueError, match="重复"):
        acc.validate_cases([a, b])


def test_load_all_rejects_duplicate_ids_across_files(tmp_path):
    d = tmp_path / "acceptance"
    shutil.copytree(FIXTURE_DIR, d)
    e_cases = json.loads((d / "E.json").read_text(encoding="utf-8"))
    dup = copy.deepcopy(e_cases[0])
    # 换一个合法 design_ref 也不行：id 重复本身就要拒绝
    e_cases.append(dup)
    (d / "E.json").write_text(json.dumps(e_cases, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        acc.load_all(d)


def test_load_all_rejects_missing_file(tmp_path):
    d = tmp_path / "acceptance"
    shutil.copytree(FIXTURE_DIR, d)
    (d / "views.json").unlink()
    with pytest.raises(ValueError, match="views.json"):
        acc.load_all(d)


def test_load_all_rejects_case_in_wrong_file(tmp_path):
    d = tmp_path / "acceptance"
    shutil.copytree(FIXTURE_DIR, d)
    a_cases = json.loads((d / "A.json").read_text(encoding="utf-8"))
    b_cases = json.loads((d / "B.json").read_text(encoding="utf-8"))
    moved = a_cases.pop()
    b_cases.append(moved)
    (d / "A.json").write_text(json.dumps(a_cases, ensure_ascii=False), encoding="utf-8")
    (d / "B.json").write_text(json.dumps(b_cases, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="B.json"):
        acc.load_all(d)


def test_load_all_rejects_unknown_json_file(tmp_path):
    d = tmp_path / "acceptance"
    shutil.copytree(FIXTURE_DIR, d)
    (d / "F.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="F.json"):
        acc.load_all(d)


# ---------- 代码风格 ----------

def _width(line):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line)


@pytest.mark.parametrize("path", [
    ROOT / "contracts" / "acceptance.py", Path(__file__),
] + [FIXTURE_DIR / f for f in FILES])
def test_line_width_at_most_110(path):
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert _width(line) <= 110, (path.name, no, _width(line))
