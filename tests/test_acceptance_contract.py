"""v3 业务验收契约（contracts.acceptance）的测试。

为什么要有这份测试：v3 第一部分的验收案例、第四部分每条链路的正例/更正/异常/判断E、6.4 的三种使用视角，
如果只写在文档里，实现者很容易用“报告写得像”蒙混过关。这里把它们固定成 JSON 夹具，并用校验器保证：
- 每个验收案例恰好出现一次，不漏、不重复；
- 每个案例都有反例和可机检的断言，禁止的捷径写进 must_not；
- 证据类型和来源编号只能用约定的词表，缺口必须如实列出；
- 夹具里不出现编造的真实世界编号（NCT、PMID、DOI 等），材料没找到就标 to_be_sourced。
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


def test_known_sources_contain_given_ids_with_right_coverage():
    available = ["clinicaltrials", "europe_pmc", "pubmed", "who_don", "sec_companyfacts", "fda_crl"]
    legacy = ["nmpa", "cde", "chictr"]
    gaps = ["cde_ctr", "cninfo", "hkex", "biorxiv", "drugs_fda"]
    for sid in available:
        assert acc.KNOWN_SOURCES[sid].coverage == "available", sid
    for sid in legacy:
        assert acc.KNOWN_SOURCES[sid].coverage == "legacy_unverified", sid
    for sid in gaps:
        assert acc.KNOWN_SOURCES[sid].coverage == "gap", sid
    given = set(available + legacy + gaps)
    introduced = {sid for sid, s in acc.KNOWN_SOURCES.items() if s.introduced_here}
    # 本模块新增的编号必须显式标出来，且不和给定编号重叠
    assert introduced and not (introduced & given)
    assert set(acc.KNOWN_SOURCES) == given | introduced
    for sid in acc.KNOWN_SOURCES:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", sid), sid


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


def test_anomaly_cases_cover_the_four_failure_modes(cases):
    anomalies = [c for c in cases if c.case_kind == "chain_anomaly"]
    assert len(anomalies) == 4
    need = {"no_duplicate_on_restart", "resumes_after_partial_failure", "no_derived_feedback_loop",
            "identifier_not_double_counted"}
    for c in anomalies:
        assert need <= kinds(c), c.id


def test_wuhan_replay_is_public_replay_with_cutoff(cases):
    d = [c for c in cases if c.design_ref == "v3 1.2 判断D 验收案例1"]
    assert len(d) == 1
    assert d[0].data_basis == "real_public_replay"
    assert "visibility_cutoff_respected" in kinds(d[0])
    assert "武汉" in d[0].question


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


def test_material_note_and_basis_honest(cases):
    for c in cases:
        assert c.material_note.strip(), c.id
    # 只有武汉回放有 v3 给出的具体真实场景；其余不得声称 real_existing
    assert not [c.id for c in cases if c.data_basis == "real_existing"]


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
