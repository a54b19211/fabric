"""v3 第二部分数据契约（store/data_objects.py）。

样例按 Codex 要求跨类型：登记结构化（真实 CT 状态变化）、附件派生正文（真实 NMPA 附件）、论文叙述（合成）、
财务表格测量（合成）、弱信号与同源更正（合成）。合成样例只验证契约和流程，不代表真实覆盖。
真实样例取自隔离库里从现役只读导出的版本，版本号和时间都是真的。
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from store import data_objects as do

UTC = timezone.utc
A, B, C, D = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)

# ——真实：NCT07902817 状态 NOT_YET_RECRUITING → RECRUITING（隔离库 fabric_history，v2 差分）——
REAL_CT = {
    "from_version_id": "8b06ab90620cc576308dd10f623b0506c63fe5003bad169655815deae1694547",
    "to_version_id": "7c68b0caf1aea24dec29e828099516ae7b619c78fc241dc8e87cbcd9238f9c93",
    "field": "status",
    "change_kind": "status_change",
    "json_pointer": "/protocolSection/statusModule/overallStatus",
}
CT_SEEN = (datetime(2026, 11, 3, 9, 23, 8, tzinfo=UTC), datetime(2026, 11, 3, 13, 32, 50, tzinfo=UTC))

# ——真实：NMPA 2020 年第 149 号公告附件（隔离库 fabric_attach，云端 MinerU 解析）——
REAL_ATTACHMENT = {
    "version_id": "03709fc771bf678f6f3cc993cb1b9a3c972b743734fbe8a04e2f065e00255584",
    "processor_version": "datafabric.attachment_parse.v2",
    "text_sha256": "b67f1726d39e51868ab7b2d02e8b5673fb09fea58a98cb92bf503ae40b01f2a2",
    "locator_kind": "converted_page",
    "status": "parsed",
    "chars": 1621,
    "segments": [{"start": 7, "end": 9, "page": 1, "type": "text", "block": 0}],
}
ATT_RECORD = ("https://www.nmpa.gov.cn/xxgk/ggtg/ypggtg/ypshmshxdgg/20201230172311185.html#attachment="
              "https://www.nmpa.gov.cn/directory/web/nmpa/images/1609320377021043567.doc")


def base(source="europe_pmc", version=A, **extra):
    return {
        "inputs": [{"source_id": source, "record_id": "MED:1", "version_id": version}],
        "processing": {"processor_version": "synthetic.extract.v1"},
        "locators": [{"kind": "char_span", "version_id": version, "start": 10, "end": 40}],
        "permission_sources": [source],
        "visibility": {"first_seen_at": "2026-11-01T00:00:00Z"},
        "status": {"state": "ok"},
        **extra,
    }


def test_real_registry_change_fits_the_contract():
    obs = do.from_registry_change(REAL_CT, source_id="clinicaltrials", record_id="NCT07902817",
                                  from_seen=CT_SEEN[0], to_seen=CT_SEEN[1],
                                  processor_version="datafabric.registry_diff.v2",
                                  description='status: "NOT_YET_RECRUITING" → "RECRUITING"')
    assert [r.role for r in obs.inputs] == ["previous", "primary"]
    assert {loc.json_pointer for loc in obs.locators} == {REAL_CT["json_pointer"]}
    assert obs.visibility.first_seen_at == CT_SEEN[1]  # 取较晚的那一版
    assert obs.visibility.proof_available_at is None  # 观察版本没有历史证明
    assert "不是成败或疗效结论" in obs.qualifiers[0]


def test_unmapped_registry_change_is_flagged_not_ok():
    change = {**REAL_CT, "field": "_unmapped", "change_kind": "unmapped_change", "json_pointer": None,
              "detail": {"changed_paths": ["/protocolSection/descriptionModule/detailedDescription"]}}
    obs = do.from_registry_change(change, source_id="clinicaltrials", record_id="NCT07902817",
                                  from_seen=CT_SEEN[0], to_seen=CT_SEEN[1],
                                  processor_version="datafabric.registry_diff.v3", description="原件有变化")
    assert obs.status.state == "unmapped"
    assert {loc.json_pointer for loc in obs.locators} == {"/protocolSection/descriptionModule/detailedDescription"}
    with pytest.raises(ValidationError):  # 没有任何真实定位的主张不收
        do.Claim.model_validate({**obs.model_dump(), "locators": [
            {"kind": "unavailable", "version_id": REAL_CT["to_version_id"]}]})


def test_real_attachment_text_fits_and_says_the_parent_version_is_unknown():
    text = do.from_clean_text(
        REAL_ATTACHMENT, source_id="nmpa", record_id=ATT_RECORD, seen=datetime(2026, 11, 4, 7, 41, tzinfo=UTC),
        links=[{"parent_record_id": ATT_RECORD.split("#")[0], "parent_version_id": None,
                "link_basis": "parent_version_unknown"}],
    )
    # 解析只读附件本身：正文是完整的；公告页是关联，版本未知如实写空和依据
    assert text.status.state == "ok" and [r.role for r in text.inputs] == ["primary"]
    [rel] = text.relations
    assert rel.version_id is None and rel.basis == "parent_version_unknown"
    [loc] = text.locators
    assert loc.kind == "converted_page" and loc.page == 1
    assert loc.derived_text_sha256 == REAL_ATTACHMENT["text_sha256"]


def test_attachment_parent_is_a_relation_not_an_input():
    seen, proof = datetime(2026, 11, 4, tzinfo=UTC), datetime(2026, 11, 4, 1, tzinfo=UTC)
    text = do.from_clean_text(
        REAL_ATTACHMENT, source_id="nmpa", record_id=ATT_RECORD, seen=seen, proof=proof,
        links=[{"parent_record_id": ATT_RECORD.split("#")[0], "parent_version_id": B,
                "link_basis": "same_run_parent_version"}],
    )
    assert [r.version_id for r in text.inputs] == [REAL_ATTACHMENT["version_id"]]
    assert [r.version_id for r in text.relations] == [B]
    # 可见时间只来自附件版本自己；父页面的时间不参与，也不能被拿来顶替
    assert text.visibility.first_seen_at == seen and text.visibility.proof_available_at == proof


def test_literature_claim_keeps_negation_and_qualifiers():
    claim = do.Claim.model_validate(base(
        statement="未见明显肝毒性（ALT ≥3×ULN 发生率 2.1%）", polarity="negated",
        qualifiers=["小鼠模型", "28 天"], speaker="作者"))
    assert claim.polarity == "negated" and "小鼠模型" in claim.qualifiers


def test_measurement_needs_unit_period_scope_and_honest_denominator():
    ok = do.Measurement.model_validate(base(
        source="cninfo", object="某公司 A 基地", metric="产能", value="1,200", unit="万支/年",
        period="2026Q2", scope="单一基地", denominator="not_applicable", measure_kind="stock",
        quote="A 基地年产能 1,200 万支"))
    assert ok.value == "1,200"  # 原样保留写法
    with pytest.raises(ValidationError):  # 没有时期
        do.Measurement.model_validate({**ok.model_dump(), "period": None})
    with pytest.raises(ValidationError):  # 比例没有分母
        do.Measurement.model_validate({**ok.model_dump(), "measure_kind": "rate"})
    with pytest.raises(ValidationError):  # 省略单位
        do.Measurement.model_validate({k: v for k, v in ok.model_dump().items() if k != "unit"})


def test_weak_signal_reposts_count_as_one_independent_source():
    reposts = [{"ref": {"source_id": "social", "record_id": f"post{i}", "version_id": v},
                "independence": "same_origin", "origin_group": "g1"} for i, v in enumerate((A, B, C))]
    official = {"ref": {"source_id": "who_don", "record_id": "DON1", "version_id": D},
                "independence": "independent"}
    event = do.DomainEvent.model_validate({
        "inputs": [r["ref"] for r in reposts] + [official["ref"]],
        "processing": {"processor_version": "synthetic.events.v1"},
        "locators": [{"kind": "char_span", "version_id": D, "start": 0, "end": 30}],
        "permission_sources": ["social", "who_don"],
        "visibility": {"first_seen_at": "2026-11-01T00:00:00Z"},
        "status": {"state": "ok"},
        "event_type": "outbreak_report", "schema_mapping": "mapped", "subjects": ["某地"],
        "support": reposts + [official],
    })
    assert event.independent_support_count() == 2  # 三条转发只算一份
    data = event.model_dump()
    with pytest.raises(ValidationError):  # 支持引用了不在输入里的版本：不能绕过权限和可见时间
        do.DomainEvent.model_validate({**data, "inputs": data["inputs"][:-1], "permission_sources": ["social"],
                                       "locators": [{"kind": "char_span", "version_id": A, "start": 0, "end": 1}]})
    with pytest.raises(ValidationError):  # 更正同理
        do.DomainEvent.model_validate({**data, "corrections": [{"source_id": "who_don", "record_id": "DON2",
                                                                 "version_id": "e" * 64}]})
    with pytest.raises(ValidationError):  # 同一版本重复列两次
        do.DomainEvent.model_validate({**data, "support": data["support"] + [data["support"][-1]]})
    with pytest.raises(ValidationError):  # 未映射却标 ok
        do.DomainEvent.model_validate({**event.model_dump(), "schema_mapping": "unmapped"})


def test_two_versions_of_one_record_are_one_independent_support():
    e = "e" * 64
    refs = [{"source_id": "who_don", "record_id": "DON1", "version_id": D},
            {"source_id": "who_don", "record_id": "DON1", "version_id": e}]
    event = do.DomainEvent.model_validate({
        "inputs": refs,
        "processing": {"processor_version": "synthetic.events.v1"},
        "locators": [{"kind": "char_span", "version_id": D, "start": 0, "end": 30}],
        "permission_sources": ["who_don"],
        "visibility": {"first_seen_at": "2026-11-01T00:00:00Z"},
        "status": {"state": "ok"},
        "event_type": "outbreak_report", "schema_mapping": "mapped", "subjects": ["某地"],
        "support": [{"ref": r, "independence": "independent"} for r in refs],
        "corrections": [refs[1]],
    })
    assert event.independent_support_count() == 1  # 原报告和它的更新版是同一份来源


def test_provenance_must_be_closed():
    with pytest.raises(ValidationError):  # 权限来源不等于输入来源
        do.Claim.model_validate(base(statement="x", polarity="affirmed", permission_sources=["pubmed"]))
    with pytest.raises(ValidationError):  # 定位指向不在输入里的版本
        do.Claim.model_validate(base(statement="x", polarity="affirmed",
                                     locators=[{"kind": "char_span", "version_id": B, "start": 0, "end": 1}]))
    with pytest.raises(ValidationError):  # 非 ok 状态没写原因
        do.Claim.model_validate(base(statement="x", polarity="affirmed", status={"state": "failed"}))
    with pytest.raises(ValidationError):  # 页码定位没给页码
        do.Locator.model_validate({"kind": "pdf_page", "version_id": A})


def test_visibility_takes_the_latest_input_and_proof_only_when_all_have_one():
    t1, t2 = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    refs = [do.VersionRef(source_id="s", record_id="r", version_id=v) for v in (A, B)]
    assert do.merge_visibility(refs, {A: (t1, t1), B: (t2, None)}).proof_available_at is None
    v = do.merge_visibility(refs, {A: (t1, t1), B: (t2, t2)})
    assert v.first_seen_at == t2 and v.proof_available_at == t2


def test_visibility_times_must_match_the_inputs_one_to_one():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    refs = [do.VersionRef(source_id="s", record_id="r", version_id=v) for v in (A, B)]
    with pytest.raises(ValueError):  # 少给一个输入的时间
        do.merge_visibility(refs, {A: (t, t)})
    with pytest.raises(ValueError):  # 拿不是输入的版本的时间顶替
        do.merge_visibility(refs, {A: (t, t), C: (t, t)})


def test_judgment_revision_must_say_why():
    first = {"topic_id": "t", "version": 1, "proposition": "三期大概率成功",
             "basis": [{"source_id": "clinicaltrials", "record_id": "NCT1", "version_id": A}],
             "invalidation_conditions": ["中期分析暂停"]}
    do.JudgmentVersion.model_validate(first)
    with pytest.raises(ValidationError):
        do.JudgmentVersion.model_validate({**first, "version": 2})
    do.JudgmentVersion.model_validate({**first, "version": 2, "previous_version": 1,
                                       "change_reason": "中期分析暂停（新版本登记）"})


def test_entity_resolution_belongs_to_the_disambiguation_owner():
    cand = base(name="XYZ-101", entity_type="Drug", identifiers={"code": "XYZ-101"})
    assert do.EntityCandidate.model_validate(cand).resolution == "candidate"
    with pytest.raises(ValidationError):
        do.EntityCandidate.model_validate({**cand, "resolution": "resolved"})


# ——测量 v2（加工层测量对象，合成样例：WHO 疫情通报的累计确诊数）——


def who_measurement(**change):
    sha = "e" * 64
    body = {
        "inputs": [{"source_id": "who_don", "record_id": "2026-DON512", "version_id": A}],
        "processing": {"processor_version": "datafabric.measure_extract.v1", "config_sha256": B},
        "locators": [{"kind": "char_span", "version_id": A, "start": 120, "end": 168, "derived_text_sha256": sha}],
        "permission_sources": ["who_don"],
        "visibility": {"first_seen_at": "2026-10-30T08:12:44Z"},
        "status": {"state": "ok"},
        "artifact": {"text_processor_version": "datafabric.clean_text.v12", "text_sha256": sha},
        "object": "Marburg virus disease", "metric": "confirmed cases", "value_text": "1444", "value_literal": "1 444",
        "unit": "cases", "period": {"type": "point", "precision": "day", "value": "2026-10-28"},
        "period_literal": "28 October 2026", "scope": "Rwanda", "scope_mode": "stated",
        "denominator_state": "not_applicable", "measure_kind": "cumulative", "qualifier": "exact",
        "modality": "actual", "family": "case_count",
        "dims": [{"kind": "disease", "value": "Marburg virus disease", "start": 120, "end": 141, "check": "match"}],
        "check": {"checker_version": "biocheck.extraction_rules.v17", "contract_version": "extraction_consistency.v3",
                  "run_id": "r-77", "status": "consistent"},
        "quote": "As of 28 October 2026, 1 444 confirmed cases of Marburg virus disease were reported in Rwanda",
        "identity": {"state": "provisional", "key": "literal:who_don|2026-DON512|marburg virus disease"},
    }
    body.update(change)
    return body


def test_measurement_v2_keeps_the_canonical_value_apart_from_the_literal():
    ok = do.MeasurementV2.model_validate(who_measurement())
    assert ok.contract_version == "datafabric.data_objects.v2" and ok.value_text == "1444"
    assert ok.value_literal == "1 444" and ok.period.precision == "day"
    with pytest.raises(ValidationError):  # the literal is not a canonical decimal
        do.MeasurementV2.model_validate(who_measurement(value_text="1 444"))
    # the v1 measurement is unchanged
    assert do.Measurement.model_fields["contract_version"].default == "datafabric.data_objects.v1"


@pytest.mark.parametrize("change", [
    {"unit": None},  # the unit is stated or "unknown", never left out
    {"period": None},  # a measurement says its period
    {"period": {"type": "point", "precision": "day", "value": "28 October"}},  # not a structured period
    {"period": {"type": "point", "precision": "month", "start": "2026-01", "end": "2026-02"}},  # a range is an interval
    {"measure_kind": "rate"},  # a rate with denominator not_applicable
    {"denominator_state": "stated"},  # stated without the denominator
    {"scope": None},  # scope_mode stated without a scope
    {"dims": [{"kind": "disease", "value": "x", "start": 1, "end": 2, "check": "match"}] * 2},  # one per kind
    {"locators": [{"kind": "char_span", "version_id": A, "start": 1, "end": 5}]},  # not on its artifact
    {"check": {"checker_version": "v", "contract_version": "c", "run_id": "r", "status": "passed"}},
])
def test_measurement_v2_refuses_what_it_cannot_state(change):
    with pytest.raises(ValidationError):
        do.MeasurementV2.model_validate(who_measurement(**change))


def test_a_rate_may_have_an_unknown_denominator():
    rate = do.MeasurementV2.model_validate(who_measurement(measure_kind="rate", denominator_state="unknown"))
    assert rate.denominator_text is None


def test_identity_is_confirmed_only_from_a_fixed_graph_package():
    graph = {"source_id": "reviewed_graph", "record_id": "bundle-17", "version_id": C}
    confirmed = do.MeasurementV2.model_validate(who_measurement(
        identity={"state": "confirmed", "key": "MESH|D008379", "graph": graph}))
    assert confirmed.identity.graph.version_id == C
    for identity in (
        {"state": "confirmed", "key": "MESH|D008379"},  # no graph package behind it
        {"state": "confirmed", "key": "literal:who_don|2026-DON512|marburg", "graph": graph},
        {"state": "provisional", "key": "MESH|D008379"},  # the same name is a record-local candidate only
        {"state": "provisional", "key": "literal:who_don|x|marburg", "graph": graph},
    ):
        with pytest.raises(ValidationError):
            do.MeasurementV2.model_validate(who_measurement(identity=identity))
