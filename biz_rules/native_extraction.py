"""Project frozen API fields into extraction payloads without an entity LLM.

FDA recall_number identifies a recalled product; event_id identifies the event:
https://www.fda.gov/safety/enforcement-reports/enforcement-report-information-and-definitions
"""
import json
import re
from dataclasses import dataclass

from biz_rules.clinical_research import (
    ClinicalTrialFacts, FIELD_PATHS, MAX_DOCUMENT_BYTES, _value, project_clinical_trial,
)
from biz_rules.history_models import sha256
from biz_rules.history_sources import DECODER, parse_json

RULE_VERSION = "native-fields.20261031.v1"
NATIVE_SOURCES = {"clinicaltrials", "fda_crl", "fda_api_enforcement", "fda_api_drug_label"}
TRIAL_ATTRIBUTE_PATHS = {
    "trial_id": FIELD_PATHS["nct_id"], "brief_title": FIELD_PATHS["brief_title"],
    "brief_summary": FIELD_PATHS["brief_summary"], "phases": FIELD_PATHS["phases"],
    "status": FIELD_PATHS["overall_status"], "why_stopped": FIELD_PATHS["why_stopped"],
    **{key: FIELD_PATHS[key] for key in ("start_date", "primary_completion_date", "completion_date",
                                        "study_first_post_date", "last_update_post_date")},
    "conditions": FIELD_PATHS["conditions"], "interventions": FIELD_PATHS["interventions"],
    "lead_sponsor": "/protocolSection/sponsorCollaboratorsModule/leadSponsor",
    "collaborators": "/protocolSection/sponsorCollaboratorsModule/collaborators",
    "enrollment": "/protocolSection/designModule/enrollmentInfo",
    "study_type": "/protocolSection/designModule/studyType",
    "primary_outcomes": "/protocolSection/outcomesModule/primaryOutcomes",
}
_MISSING = object()


class NativeExtractionError(ValueError):
    """The native source is absent, malformed or cannot establish its ID grain."""


def field_value(data, pointer, default=None):
    node = data
    try:
        for token in pointer.split("/")[1:]:
            token = token.replace("~1", "/").replace("~0", "~")
            node = node[int(token)] if isinstance(node, list) else node[token]
        return node
    except (KeyError, TypeError, IndexError, ValueError):
        return default


def clinical_trial_attributes(data):
    """Keep source arrays, date precision, ACTUAL/ESTIMATED and explicit nulls."""
    protocol = data.get("protocolSection")
    if not isinstance(protocol, dict):
        raise NativeExtractionError("缺少原始protocolSection JSON")
    values = {key: _value(protocol, path) for key, path in FIELD_PATHS.items()}
    # The existing research validator checks scalar/array/date types. Additional
    # intervention fields stay in the source payload rather than being inferred.
    if isinstance(values.get("interventions"), list):
        values["interventions"] = [{key: item.get(key) for key in ("type", "name")}
                                   for item in values["interventions"]]
    for key in ("start_date", "primary_completion_date", "completion_date",
                "study_first_post_date", "last_update_post_date"):
        if isinstance(values.get(key), dict):
            values[key] = {field: values[key].get(field) for field in ("date", "type")}
    ClinicalTrialFacts.model_validate(values)
    attributes = {key: field_value(data, path) for key, path in TRIAL_ATTRIBUTE_PATHS.items()}
    for key in ("lead_sponsor",):
        if attributes[key] is not None and not isinstance(attributes[key], dict):
            raise NativeExtractionError("原始申办方字段类型错误")
    for key in ("collaborators", "primary_outcomes"):
        if attributes[key] is not None and (not isinstance(attributes[key], list)
                                           or any(not isinstance(item, dict) for item in attributes[key])):
            raise NativeExtractionError("原始登记数组字段类型错误")
    enrollment = attributes["enrollment"]
    if enrollment is not None:
        count = enrollment.get("count") if isinstance(enrollment, dict) else False
        if count is not None and (type(count) is not int or count < 0):
            raise NativeExtractionError("原始入组人数类型错误")
    return attributes


def _index_json(raw, data, start=0, pointer=""):
    """Index exact token spans in the original Unicode string, including escapes."""
    spans = {}

    def walk(node, offset, path):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        begin = offset
        if isinstance(node, dict):
            offset += 1
            for index, (key, value) in enumerate(node.items()):
                while raw[offset].isspace():
                    offset += 1
                if index:
                    if raw[offset] != ',':
                        raise NativeExtractionError("原始JSON对象无法定位")
                    offset += 1
                    while raw[offset].isspace():
                        offset += 1
                parsed_key, offset = DECODER.raw_decode(raw, offset)
                while raw[offset].isspace():
                    offset += 1
                if parsed_key != key or raw[offset] != ':':
                    raise NativeExtractionError("原始JSON字段无法定位")
                token = key.replace("~", "~0").replace("/", "~1")
                offset = walk(value, offset + 1, path + "/" + token)
            while raw[offset].isspace():
                offset += 1
            if raw[offset] != '}':
                raise NativeExtractionError("原始JSON对象未结束")
            offset += 1
        elif isinstance(node, list):
            offset += 1
            for index, value in enumerate(node):
                while raw[offset].isspace():
                    offset += 1
                if index:
                    if raw[offset] != ',':
                        raise NativeExtractionError("原始JSON数组无法定位")
                    offset += 1
                offset = walk(value, offset, path + "/" + str(index))
            while raw[offset].isspace():
                offset += 1
            if raw[offset] != ']':
                raise NativeExtractionError("原始JSON数组未结束")
            offset += 1
        else:
            _value, offset = DECODER.raw_decode(raw, offset)
        spans[path] = (begin, offset)
        return offset

    walk(data, start, pointer)
    return spans


@dataclass
class NativeSource:
    raw: str
    data: dict
    spans: dict
    source_site: str
    source_version_id: str | None
    source_ref: dict

    def evidence(self, pointer):
        start, end = self.spans[pointer]
        locator = {"field": "raw_content", "offset_unit": "unicode_codepoint", "json_pointer": pointer}
        if self.source_ref.get("source_id") == "legacy_documents":
            locator["container_json_pointer"] = "/original_raw_content"
        return {**self.source_ref, "source_version_id": self.source_version_id,
                "source_quote": self.raw[start:end], "source_char_start": start, "source_char_end": end,
                "rule_version": RULE_VERSION,
                "locator": locator,
                "value": field_value(self.data, pointer)}


def _source(raw, site, source_version_id=None, source_ref=None):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise NativeExtractionError("原始结构化资料为空或超出大小限制")
    if raw.lstrip().startswith("{"):
        data = parse_json(raw)
        spans = _index_json(raw, data)
    elif site == "clinicaltrials":
        projection = project_clinical_trial(raw)
        if projection.status != "available":
            raise NativeExtractionError("需要原始protocolSection JSON: " + str(projection.reason))
        protocol = parse_json(projection.text)
        data = {"protocolSection": protocol}
        spans = _index_json(raw, protocol, projection.locator.start, "/protocolSection")
    else:
        # Match only the exact original API formatter labels, never titles or
        # prose. Nested dictionaries/lists remain the original JSON token text.
        markers = list(re.finditer(r"(?m)^\*\*([A-Za-z_][A-Za-z0-9_]*)\*\*:[ \t]*", raw))
        data, spans = {}, {}
        for index, marker in enumerate(markers):
            key = marker.group(1)
            if key in data:
                raise NativeExtractionError("原始API字段重复，无法唯一定位")
            start = marker.end()
            end = markers[index + 1].start() if index + 1 < len(markers) else len(raw)
            while end > start and raw[end - 1] in "\r\n":
                end -= 1
            text = raw[start:end]
            if text.lstrip().startswith(("{", "[")):
                value = DECODER.decode(text)
                spans.update(_index_json(raw, value, start, "/" + key))
            else:
                value = text
                spans["/" + key] = (start, end)
            data[key] = value
        if not data:
            raise NativeExtractionError("需要原始API JSON或原字段附录，不从标题猜编号")
    source_ref = dict(source_ref or {})
    known_version = source_ref.get("version_id") or source_ref.get("source_version_id")
    if source_version_id and known_version and source_version_id != known_version:
        raise NativeExtractionError("原始版本引用矛盾")
    return NativeSource(raw, data, spans, site, source_version_id or known_version, source_ref)


def _project(source):
    entities, relations, seen = [], [], {}
    seen_relations = {}

    def entity(kind, name, data, pointer, *, identity_namespace=None, identity_value=None, paths=()):
        evidence = [source.evidence(path) for path in dict.fromkeys([pointer, *paths]) if path in source.spans]
        key = kind, name
        if key in seen:
            existing = entities[seen[key]]
            existing["evidence"].extend(item for item in evidence if item not in existing["evidence"])
            existing["entity_data"]["_native"]["field_evidence"] = existing["evidence"]
            return seen[key]
        native = {"rule_version": RULE_VERSION, "identity_status": "rule_confirmed" if identity_namespace else "unresolved",
                  "identity_namespace": identity_namespace, "identity_value": identity_value,
                  "source_ref": {**source.source_ref, "source_version_id": source.source_version_id},
                  "origin_raw_sha256": sha256(source.raw),
                  "field_evidence": evidence,
                  "limitations": ["来源规则投影不等于人工审核；名称端点尚未确认；历史版本未知时不能作为固定历史证明"]}
        row = {"entity_type": kind, "entity_name": name,
               "entity_data": {**data, "_native": native}, "dict_id": None,
               "source_text": source.raw[source.spans[pointer][0]:source.spans[pointer][1]],
               "confidence_score": 1.0, "confidence_reason": "source field projection; not an identity probability",
               "first_seen_at": None, "valid_from": None, "valid_to": None,
               **source.evidence(pointer), "evidence": evidence}
        seen[key] = len(entities)
        entities.append(row)
        return seen[key]

    def relation(source_index, target_index, kind, pointer):
        evidence = source.evidence(pointer)
        key = source_index, target_index, kind
        if key in seen_relations:
            existing = relations[seen_relations[key]]
            if evidence not in existing["evidence"]:
                existing["evidence"].append(evidence)
            return
        seen_relations[key] = len(relations)
        evidence_list = [evidence]
        relations.append({"from_entity_idx": source_index, "to_entity_idx": target_index,
                          "relation_type": kind, "relation_props": {"_native": {"rule_version": RULE_VERSION,
                              "field_evidence": evidence_list, "identity_status": "unresolved"}},
                          "source_text": evidence["source_quote"], "confidence_score": 1.0,
                          "confidence_reason": "registered source field; endpoints require identity review",
                          "first_seen_at": None, "valid_from": None, "valid_to": None,
                          **evidence, "evidence": evidence_list})

    if source.source_site == "clinicaltrials":
        attributes = clinical_trial_attributes(source.data)
        nct = attributes["trial_id"]
        trial = entity("ClinicalTrial", nct, attributes, FIELD_PATHS["nct_id"],
                       identity_namespace="clinicaltrials.NCT", identity_value=nct,
                       paths=TRIAL_ATTRIBUTE_PATHS.values())
        entries = []
        for index, value in enumerate(attributes["conditions"] or []):
            entries.append(("Indication", value, {"name": value}, "REGISTERED_CONDITION",
                            f"/protocolSection/conditionsModule/conditions/{index}", None))
        for index, value in enumerate(attributes["interventions"] or []):
            entries.append(("Intervention", value.get("name"), {"name": value.get("name"), "source_fields": value},
                            "REGISTERED_INTERVENTION", f"/protocolSection/armsInterventionsModule/interventions/{index}/name",
                            f"/protocolSection/armsInterventionsModule/interventions/{index}"))
        for key, kind in (("lead_sponsor", "REGISTERED_SPONSOR"), ("collaborators", "REGISTERED_COLLABORATOR")):
            values = [attributes[key]] if key == "lead_sponsor" and attributes[key] else attributes[key] or []
            for index, value in enumerate(values):
                pointer = TRIAL_ATTRIBUTE_PATHS[key] + (f"/{index}" if key == "collaborators" else "")
                entries.append(("Organization", value.get("name"), {"name": value.get("name"), "source_fields": value},
                                kind, pointer + "/name", pointer))
        for kind, name, data, relation_kind, pointer, object_path in entries:
            if not isinstance(name, str) or not name.strip():
                raise NativeExtractionError("原始登记名称缺失或类型错误，不推断端点")
            target = entity(kind, name, data, pointer, paths=[object_path] if object_path else [])
            relation(trial, target, relation_kind, pointer)
    elif source.source_site == "fda_api_enforcement":
        number = source.data.get("recall_number")
        if not isinstance(number, str) or not re.fullmatch(r"D-[0-9]+-[0-9]{4}", number):
            raise NativeExtractionError("缺少可核对的原始FDA药品召回产品编号")
        data = {key: source.data.get(key) for key in ("recall_number", "event_id", "classification", "status",
                "recall_initiation_date", "termination_date", "report_date", "recalling_firm", "product_description",
                "reason_for_recall", "distribution_pattern", "code_info")}
        data["external_ids"] = {"fda.recall_number": number}
        entity("RecallProduct", number, data, "/recall_number", identity_namespace="fda.recall_number",
               identity_value=number, paths=["/" + key for key in data if key != "external_ids"])
    else:
        pointer = "/application_number" if source.source_site == "fda_crl" else "/openfda/application_number"
        numbers = field_value(source.data, pointer)
        if not isinstance(numbers, list):
            raise NativeExtractionError("缺少原始FDA申请编号数组")
        fields = {key: source.data[key] for key in ("letter_date", "letter_type", "file_name", "company_name",
                  "approval_status", "text", "set_id", "effective_time", "openfda") if key in source.data}
        for index, raw_number in enumerate(numbers):
            if not isinstance(raw_number, str) or not re.fullmatch(r"(?:NDA|ANDA|BLA) *[0-9]{6}", raw_number):
                continue
            number = raw_number.replace(" ", "")
            data = {"application_number": number, "application_number_raw": raw_number,
                    "authority": "FDA", "source_fields": fields,
                    "external_ids": {"fda.application_number": number}}
            target = entity("RegulatoryApplication", number, data, pointer + f"/{index}",
                            identity_namespace="fda.application_number", identity_value=number,
                            paths=["/" + key for key in fields])
            entities[target]["entity_data"]["_native"]["normalization"] = "NDA/ANDA/BLA prefix plus six digits; remove ASCII spaces"
        if not entities:
            raise NativeExtractionError("无可核对的FDA申请编号；monograph引用及叙述不是申请身份")
    return {"entities": entities, "relations": relations, "rule_version": RULE_VERSION}


def extract_native_entities(raw_content, source_site, *, source_version_id=None, source_ref=None):
    """Only known structured sites enter here; missing native fields fail closed."""
    if source_site not in NATIVE_SOURCES:
        raise NativeExtractionError("未实现该来源的确定性原始字段映射")
    try:
        return _project(_source(raw_content, source_site, source_version_id, source_ref))
    except NativeExtractionError:
        raise
    except Exception as error:
        raise NativeExtractionError("原始结构化字段无效或无法精确定位: " + type(error).__name__) from None
