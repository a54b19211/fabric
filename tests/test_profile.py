"""v3 3.3 分类、四维质量与分流（clean/profile.py）。确定性规则，不调模型。"""

from clean import profile as pf


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
    p = run("clinicaltrials", warnings=["field_truncated:locations", "html_sup_sub_unpaired"])
    assert p["quality"]["format"]["state"] == "degraded" and p["route"] == "normal"


def test_unknown_source_is_honest():
    p = run("some_future_source")
    assert p["quality"]["source_credibility"]["state"] == "unknown"
    assert p["evidence_types"] == [] and p["decisions"] == []


def test_experiment_types_are_candidates_with_their_basis_and_place_in_the_text():
    """v3 6.2: in vitro / animal / human, several at once, each with the rule and where in the cleaned text;
    a registry record is human by its source, a regulatory notice is not research, no signal is unknown."""
    text = "XYZ-101 inhibited growth in cell lines and in a mouse xenograft model; 40 patients enrolled."
    found = {e["type"]: e for e in pf.experiment_types("europe_pmc", None, text)}
    assert set(found) == {"in_vitro", "animal", "human"}
    for e in found.values():
        assert e["basis"] == pf.EXPERIMENT_RULE
        assert all(text[loc["start"]:loc["end"]] == loc["text"] for loc in e["locators"])
    assert [loc["text"] for loc in found["animal"]["locators"]] == ["mouse", "xenograft"]
    assert pf.experiment_types("clinicaltrials", None, "") == [
        {"type": "human", "basis": "registry_record", "locators": []}]
    assert pf.experiment_types("nmpa", None, "患者") == [
        {"type": "not_applicable", "basis": "source_is_not_research", "locators": []}]
    assert pf.experiment_types("nmpa", "attachment", "小鼠实验")[0]["type"] == "animal"  # an attachment is read
    assert pf.experiment_types("europe_pmc", None, "A review of policy.") == [
        {"type": "unknown", "basis": pf.EXPERIMENT_RULE, "locators": []}]


def test_reading_level_is_what_fabric_has_not_what_a_model_read():
    assert pf.reading_level("europe_pmc", None, 900, [], None) == {
        "level": "abstract", "basis": "literature_record", "chars": 900}
    assert pf.reading_level("europe_pmc", None, 40, ["no_abstract"], None)["level"] == "title_only"
    assert pf.reading_level("pubmed", "attachment", 90000, [], "parsed")["level"] == "full_text"
    assert pf.reading_level("nmpa", "attachment", 0, [], "failed")["level"] == "none"
    assert pf.reading_level("clinicaltrials", None, 2400, [], None)["level"] == "registry_record"
    assert pf.reading_level("nmpa", None, 300, [], None)["level"] == "page_text"
    got = pf.profile(source_id="europe_pmc", chars=10, warnings=[], identifiers=[], text="in vitro")
    assert got["processor_version"] == "datafabric.profile.v5"
    assert got["experiment_types"][0]["type"] == "in_vitro" and got["reading_level"]["level"] == "abstract"
