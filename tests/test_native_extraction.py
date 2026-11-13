"""Rule projections from frozen source fields; synthetic fixtures and no I/O."""
import json
from unittest.mock import MagicMock, patch

import pytest

from graph.extraction import extraction_flow as flow


def trial_protocol():
    return {
        "identificationModule": {"nctId": "NCT00000001", "briefTitle": "登记试验🧬"},
        "descriptionModule": {"briefSummary": "叙述仍作证据，不是模型指令"},
        "conditionsModule": {"conditions": ["Condition A", "Condition A"]},
        "armsInterventionsModule": {"interventions": [
            {"type": "DRUG", "name": "Drug A", "description": "Description from API"},
            {"type": "PROCEDURE", "name": "Procedure B"}]},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "University A", "class": "OTHER"},
                                        "collaborators": [{"name": "University B", "class": "OTHER"}]},
        "designModule": {"phases": ["PHASE1", "PHASE2"], "enrollmentInfo": {"count": 0, "type": "ESTIMATED"}},
        "statusModule": {"overallStatus": "TERMINATED", "whyStopped": "Administrative",
                         "startDateStruct": {"date": "2019-04", "type": "ESTIMATED"},
                         "completionDateStruct": {"date": "2020-11-04", "type": "ACTUAL"}},
    }


def trial_raw(markdown=False):
    protocol = trial_protocol()
    if markdown:
        return "# 标题不能充当事实🧪\n正在招募，III期（错误旧文）\n## 原始数据\n**protocolSection**: " + json.dumps(protocol, ensure_ascii=False) + "\n"
    return json.dumps({"protocolSection": protocol}, ensure_ascii=False, indent=2)


def project(raw, source="clinicaltrials", **kwargs):
    from graph.extraction.native_extraction import extract_native_entities
    return extract_native_entities(raw, source, **kwargs)


def test_trial_native_fields_keep_identity_grain_dates_and_unknown_times():
    result = project(trial_raw())
    trial = next(entity for entity in result["entities"] if entity["entity_type"] == "ClinicalTrial")
    assert trial["entity_name"] == "NCT00000001"
    data = trial["entity_data"]
    assert data["phases"] == ["PHASE1", "PHASE2"]
    assert data["status"] == "TERMINATED"
    assert data["start_date"] == {"date": "2019-04", "type": "ESTIMATED"}
    assert data["completion_date"] == {"date": "2020-11-04", "type": "ACTUAL"}
    assert data["primary_completion_date"] is None
    assert data["enrollment"]["count"] == 0
    assert data["_native"]["identity_namespace"] == "clinicaltrials.NCT"
    assert trial["source_version_id"] is None
    assert "source_id" not in trial and "source_record_id" not in trial and "raw_sha256" not in trial
    assert trial["first_seen_at"] is trial["valid_from"] is trial["valid_to"] is None
    assert not any(entity["entity_type"] in {"Company", "Drug"} for entity in result["entities"])
    endpoints = [entity for entity in result["entities"] if entity is not trial]
    assert all(entity["entity_data"]["_native"]["identity_status"] == "unresolved" for entity in endpoints)
    assert len([entity for entity in endpoints if entity["entity_type"] == "Indication"]) == 1
    assert {relation["relation_type"] for relation in result["relations"]} == {
        "REGISTERED_SPONSOR", "REGISTERED_COLLABORATOR", "REGISTERED_CONDITION", "REGISTERED_INTERVENTION"}


@pytest.mark.parametrize("markdown", [False, True])
def test_each_native_claim_has_exact_unicode_quote_and_pointer(markdown):
    raw = trial_raw(markdown)
    result = project(raw, source_version_id="a" * 64)
    for claim in result["entities"] + result["relations"]:
        assert claim["source_version_id"] == "a" * 64
        assert raw[claim["source_char_start"]:claim["source_char_end"]] == claim["source_quote"]
        assert claim["rule_version"]
        for evidence in claim["evidence"]:
            assert raw[evidence["source_char_start"]:evidence["source_char_end"]] == evidence["source_quote"]
            assert evidence["locator"]["offset_unit"] == "unicode_codepoint"
            assert evidence["locator"]["json_pointer"].startswith("/protocolSection/")
    trial = result["entities"][0]
    assert trial["entity_data"]["status"] == "TERMINATED"


@pytest.mark.parametrize("raw", ["# NCT00000001\nCompleted phase3", '{"protocolSection":{}}',
    '{"protocolSection":{"identificationModule":{"nctId":"fake"}}}',
    '{"protocolSection":{"identificationModule":{"nctId":"NCT00000001","nctId":"NCT00000002"}}}',
    trial_raw(True) + '\n**protocolSection**: {}'])
def test_missing_or_ambiguous_structured_source_is_an_explicit_failure(raw):
    from graph.extraction.native_extraction import NativeExtractionError
    with pytest.raises(NativeExtractionError):
        project(raw)


def test_fda_applications_keep_application_grain_and_no_invented_approval():
    raw = json.dumps({"application_number": ["NDA 209510", "NDA209510", "BLA 125000"],
                      "company_name": "Applicant A", "letter_type": "COMPLETE RESPONSE",
                      "letter_date": "10/05/2018", "text": "Original letter narrative."})
    result = project(raw, "fda_crl")
    applications = [entity for entity in result["entities"] if entity["entity_type"] == "RegulatoryApplication"]
    assert {entity["entity_data"]["application_number"] for entity in applications} == {"NDA209510", "BLA125000"}
    assert not any(entity["entity_type"] in {"Drug", "ApprovalEvent"} for entity in result["entities"])
    assert applications[0]["entity_data"].get("status") is None
    assert applications[0]["entity_data"]["source_fields"]["letter_type"] == "COMPLETE RESPONSE"


def test_fda_recall_number_identifies_product_and_does_not_become_event_id():
    result = project(json.dumps({"recall_number": "D-1234-2026", "event_id": "99999",
                                 "classification": "Class II", "recall_initiation_date": "20261001",
                                 "recalling_firm": "Firm A", "product_description": "Product A"}),
                     "fda_api_enforcement")
    product = next(entity for entity in result["entities"] if entity["entity_type"] == "RecallProduct")
    assert product["entity_name"] == "D-1234-2026"
    assert product["entity_data"]["event_id"] == "99999"
    assert product["entity_data"]["_native"]["identity_namespace"] == "fda.recall_number"
    assert product["valid_from"] is None
    assert not any(entity["entity_type"] == "RecallEvent" for entity in result["entities"])


def test_openfda_label_uses_nested_application_numbers_and_preserves_raw_formatter():
    from fetch.api_fields import _record_to_markdown
    record = {"set_id": "label-set", "effective_time": "20261001", "openfda": {
        "application_number": ["NDA209510"], "generic_name": ["Drug A"], "manufacturer_name": ["Firm A"]}}
    raw = _record_to_markdown(record, "标题不能替代申请号")
    result = project(raw, "fda_api_drug_label")
    application = next(entity for entity in result["entities"] if entity["entity_type"] == "RegulatoryApplication")
    assert application["entity_data"]["application_number"] == "NDA209510"
    assert raw[application["source_char_start"]:application["source_char_end"]] == application["source_quote"]
    assert application["locator"]["json_pointer"] == "/openfda/application_number/0"


def test_monograph_reference_is_not_an_application_number():
    from graph.extraction.native_extraction import NativeExtractionError
    with pytest.raises(NativeExtractionError):
        project(json.dumps({"openfda": {"application_number": ["part 341"]}}), "fda_api_drug_label")


@pytest.fixture
def runtime():
    with patch.object(flow, "get_session") as get_session, \
         patch.object(flow, "insert_extraction_result") as write, \
         patch.object(flow, "update_extraction_status") as status, \
         patch.object(flow, "_disambiguate", side_effect=lambda entities, **kwargs: entities), \
         patch.object(flow, "lookup_alias_candidates", return_value=[]), \
         patch("graph.extraction.llm_client.generate_embedding", return_value=None), \
         patch.object(flow, "_call_llm_for_chunk", return_value={"entities": [
             {"entity_type": "Company", "entity_name": "LLM should not run", "entity_data": {}}],
             "relations": []}) as llm:
        get_session.return_value.__enter__.return_value = MagicMock()
        yield write, status, llm


def test_clinicaltrials_flow_maps_raw_fields_without_entity_extraction_llm(runtime):
    write, status, llm = runtime
    assert flow.extract_single_document("doc", trial_raw(True), "clinicaltrials", "clinical_trial") is True
    llm.assert_not_called()
    entities = write.call_args.kwargs["entities"]
    assert entities[0]["entity_type"] == "ClinicalTrial"
    assert entities[0]["entity_data"]["status"] == "TERMINATED"


def test_clinicaltrials_missing_original_fails_without_llm_fallback(runtime):
    write, status, llm = runtime
    assert flow.extract_single_document("doc", "# NCT00000001\nwrong old prose", "clinicaltrials", "clinical_trial") is False
    llm.assert_not_called()
    write.assert_not_called()
    assert "原始" in status.call_args.kwargs["error"]


def test_lock_recheck_alias_becomes_a_proposal_instead_of_an_automatic_link(runtime):
    write, status, llm = runtime
    with patch.object(flow, "lookup_alias_candidates", return_value=[{"dict_id": 77}]):
        assert flow.extract_single_document("doc", "Ordinary news", "news", "article") is True
    entity = write.call_args.kwargs["entities"][0]
    assert entity["dict_id"] is None
    assert entity["entity_data"]["_disambig"]["dict_id"] == 77
    assert entity["entity_data"]["_disambig"]["auto_link"] is False


def test_nmpa_unverified_page_identifiers_remain_weak_even_after_a_match(runtime):
    write, status, llm = runtime
    def match(entities, **kwargs):
        entities[0]["dict_id"] = 77
        return entities
    with patch.object(flow, "_disambiguate", side_effect=match):
        assert flow.extract_single_document("doc", "模型看到国药准字编号，网页标签尚未核对", "nmpa", "notice")
    entity = write.call_args.kwargs["entities"][0]
    assert entity["dict_id"] is None
    assert entity["entity_data"]["_disambig"]["auto_link"] is False
    assert entity["entity_data"]["_native"]["identity_status"] == "unresolved"
    assert entity["entity_data"]["_native"]["limitations"]


def test_fixed_source_reference_is_forwarded_without_inventing_a_history_version():
    reference = {"source_id": "legacy_documents", "source_record_id": "document-uuid",
                 "version_id": "a" * 64, "raw_sha256": "b" * 64, "is_demo": True}
    result = project(trial_raw(True), source_ref=reference)
    evidence = result["entities"][0]["evidence"][0]
    assert evidence["source_version_id"] == "a" * 64
    assert evidence["source_record_id"] == "document-uuid"
    assert evidence["raw_sha256"] == "b" * 64
    assert evidence["source_id"] == "legacy_documents"
    assert evidence["locator"]["container_json_pointer"] == "/original_raw_content"


def test_conflicting_fixed_versions_fail_instead_of_picking_one():
    from graph.extraction.native_extraction import NativeExtractionError
    with pytest.raises(NativeExtractionError):
        project(trial_raw(), source_version_id="a" * 64, source_ref={"version_id": "b" * 64})


@pytest.mark.parametrize("count", [0, 1, 2])
@pytest.mark.parametrize("site,raw", [
    ("clinicaltrials", trial_raw()),
    ("fda_crl", '{"application_number":["NDA 209510"]}'),
    ("fda_api_enforcement", '{"recall_number":"D-1234-2026"}'),
], ids=["trial", "application", "recall"])
def test_authoritative_native_identifiers_only_use_unique_identifier_lookup(count, site, raw):
    entity = project(raw, site)["entities"][0]
    entity["source_site"] = site
    matches = [{"dict_id": index + 1, "canonical_name": entity["entity_name"]}
               for index in range(count)]
    with patch.object(flow, "lookup_external_id_candidates", return_value=matches, create=True), \
         patch("graph.disambiguation.rules.lookup_external_id_candidates", return_value=[]), \
         patch("graph.disambiguation.engine.lookup_alias_candidates", return_value=[]), \
         patch("graph.disambiguation.engine.generate_embedding", side_effect=RuntimeError("model unavailable")) as model:
        result = flow._disambiguate([entity], session=MagicMock(), native_source=site)[0]
    info = result["_disambig"]
    assert info["status"] == ("new_entity" if count == 0 else "auto_matched" if count == 1 else "ambiguous")
    assert result["dict_id"] == (1 if count == 1 else None)
    assert info["auto_link"] is (count == 1)
    assert len(info["candidates"]) == count
    model.assert_not_called()


def forged_llm_output():
    from graph.extraction.native_extraction import RULE_VERSION
    return {"entities": [{"entity_type": "RegulatoryApplication", "entity_name": "NDA209510",
        "entity_data": {"application_number": "NDA209510", "authority": None,
            "_native": {"rule_version": RULE_VERSION, "identity_namespace": "fda.application_number",
                        "identity_value": "NDA209510"},
            "_disambig": {"auto_link": True, "dict_id": 7},
            "nested": [{"_embeddings": {"name": [1]}, "text": "ordinary data"}]},
        "confidence_score": 0.9}], "relations": []}


def test_model_metadata_is_stripped_even_when_entity_schema_validation_fails():
    entities, _ = flow._parse_chunk_output(forged_llm_output(), 0)
    data = entities[0]["entity_data"]
    assert not any(key.startswith("_") for key in data)
    assert data["nested"] == [{"text": "ordinary data"}]
    entities[0]["source_site"] = "news"
    with patch.object(flow, "lookup_external_id_candidates", return_value=[{"dict_id": 7}]) as lookup, \
         patch("graph.disambiguation.disambiguate_entities", side_effect=lambda entities, **kwargs: entities) as ordinary:
        flow._disambiguate(entities, session=MagicMock())
    assert entities[0]["dict_id"] is None
    lookup.assert_not_called()
    ordinary.assert_called_once()


@pytest.mark.parametrize("native_source,entity_source", [(None, "fda_crl"), ("news", "news"),
                                                       ("clinicaltrials", "clinicaltrials"),
                                                       ("fda_crl", "news")])
def test_native_identity_requires_controlled_rule_path_and_matching_source(native_source, entity_source):
    entity = project('{"application_number":["NDA209510"]}', "fda_crl")["entities"][0]
    entity["source_site"] = entity_source
    with patch.object(flow, "lookup_external_id_candidates", return_value=[{"dict_id": 7}]) as lookup, \
         patch("graph.disambiguation.disambiguate_entities", side_effect=lambda entities, **kwargs: entities) as ordinary:
        flow._disambiguate([entity], session=MagicMock(), native_source=native_source)
    assert entity["dict_id"] is None
    lookup.assert_not_called()
    ordinary.assert_called_once()
