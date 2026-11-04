"""v3 3.3 分类、四维质量与分流（processing/profile.py）。确定性规则，不调模型。"""

from processing import profile as pf


def run(source, *, chars=800, warnings=(), role=None, raw_types=None, parse_status=None):
    return pf.profile(source_id=source, role=role, chars=chars, warnings=list(warnings),
                      identifiers=[{"kind": "nct", "value": "NCT01234567"}], pub_types=raw_types,
                      parse_status=parse_status)


def test_four_dimensions_are_separate_and_unassessed_ones_say_so():
    p = run("clinicaltrials")
    assert set(p["quality"]) == {"format", "source_credibility", "entity_match", "fact_check"}
    assert p["quality"]["format"]["state"] == "ok"
    assert p["quality"]["source_credibility"]["state"] == "official_registry"
    # 消歧和事实核验不在这一层：如实写未评估，不给分数
    assert p["quality"]["entity_match"] == {"state": "not_assessed", "identifiers": 1,
                                             "reason": "实体消歧由唐的图谱负责"}
    assert p["quality"]["fact_check"]["state"] == "not_checked"
    assert not any(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for d in p["quality"].values() for k, v in d.items() if k != "identifiers")


def test_classification_comes_from_the_catalog_and_the_record_itself():
    ct = run("clinicaltrials")
    assert ct["doc_type"] == "registry_record" and ct["evidence_types"] == ["registry_record"]
    assert set(ct["decisions"]) == {"A", "B", "E"}
    review = run("europe_pmc", raw_types=["review", "Journal Article"])
    assert review["doc_type"] == "review"
    research = run("europe_pmc", raw_types=["research-article", "Journal Article"])
    assert research["doc_type"] == "primary_research"
    assert run("europe_pmc", raw_types=None)["doc_type"] == "literature_unknown_type"
    assert run("nmpa", role="web_capture")["doc_type"] == "regulatory_notice"
    assert run("nmpa", role="attachment")["doc_type"] == "attachment"
    assert run("who_don")["doc_type"] == "official_notice"


def test_short_or_unverified_material_is_a_weak_signal_not_quarantined():
    """Codex：缺正文、弱来源、格式错误分别记录，不能把“弱”当隔离理由。"""
    short = run("europe_pmc", chars=90, warnings=["no_abstract"], raw_types=["research-article"])
    assert short["route"] == "weak_signal"
    assert short["quality"]["format"]["state"] == "degraded"
    assert "no_abstract" in short["quality"]["format"]["reasons"]
    assert any("短" in r for r in short["route_reasons"])


def test_only_broken_input_is_quarantined_and_metadata_stays():
    empty = run("nmpa", role="web_capture", chars=0, warnings=["no_body"])
    assert empty["route"] == "quarantine" and empty["quality"]["format"]["state"] == "broken"
    failed = run("nmpa", role="attachment", chars=0, parse_status="failed")
    assert failed["route"] == "quarantine"
    assert failed["doc_type"] == "attachment"  # 元数据照常保留


def test_truncation_and_flattened_markup_are_degraded_not_broken():
    p = run("clinicaltrials", warnings=["field_truncated:locations", "html_sup_sub_flattened"])
    assert p["quality"]["format"]["state"] == "degraded" and p["route"] == "normal"


def test_unknown_source_is_honest():
    p = run("some_future_source")
    assert p["quality"]["source_credibility"]["state"] == "unknown"
    assert p["evidence_types"] == [] and p["decisions"] == []
