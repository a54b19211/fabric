"""Project the stored ClinicalTrials protocol without rewriting historical data."""

from processing.identifiers import NCT_ID
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROJECTION_VERSION = "clinicaltrials.protocol.v1"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
FIELD_PATHS = {
    "nct_id": "/protocolSection/identificationModule/nctId",
    "brief_title": "/protocolSection/identificationModule/briefTitle",
    "brief_summary": "/protocolSection/descriptionModule/briefSummary",
    "conditions": "/protocolSection/conditionsModule/conditions",
    "interventions": "/protocolSection/armsInterventionsModule/interventions",
    "phases": "/protocolSection/designModule/phases",
    "overall_status": "/protocolSection/statusModule/overallStatus",
    "why_stopped": "/protocolSection/statusModule/whyStopped",
    "study_first_post_date": "/protocolSection/statusModule/studyFirstPostDateStruct",
    "last_update_post_date": "/protocolSection/statusModule/lastUpdatePostDateStruct",
    "start_date": "/protocolSection/statusModule/startDateStruct",
    "primary_completion_date": "/protocolSection/statusModule/primaryCompletionDateStruct",
    "completion_date": "/protocolSection/statusModule/completionDateStruct",
}


class SourceValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ClinicalTrialDate(SourceValue):
    # Preserve source precision and ACTUAL/ESTIMATED, without inventing an instant.
    date: Optional[str] = None
    type: Optional[str] = None


class ClinicalTrialIntervention(SourceValue):
    type: Optional[str] = None
    name: Optional[str] = None


class ClinicalTrialFacts(SourceValue):
    nct_id: str = Field(pattern="^" + NCT_ID + "$")
    brief_title: Optional[str] = None
    brief_summary: Optional[str] = None
    conditions: Optional[list[str]] = None
    interventions: Optional[list[ClinicalTrialIntervention]] = None
    phases: Optional[list[str]] = None
    overall_status: Optional[str] = None
    why_stopped: Optional[str] = None
    study_first_post_date: Optional[ClinicalTrialDate] = None
    last_update_post_date: Optional[ClinicalTrialDate] = None
    start_date: Optional[ClinicalTrialDate] = None
    primary_completion_date: Optional[ClinicalTrialDate] = None
    completion_date: Optional[ClinicalTrialDate] = None


class ProtocolLocator(SourceValue):
    field: Literal["raw_content"] = "raw_content"
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"


class ClinicalTrialProjection(SourceValue):
    version: Literal["clinicaltrials.protocol.v1"] = PROJECTION_VERSION
    status: Literal["available", "unavailable"]
    reason: Optional[str] = None
    text: Optional[str] = None
    locator: Optional[ProtocolLocator] = None
    facts: Optional[ClinicalTrialFacts] = None
    field_paths: dict[str, str] = Field(default_factory=dict)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON member")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-JSON numeric constant")


def _value(protocol: dict, path: str):
    node = protocol
    for part in path.removeprefix("/protocolSection/").split("/"):
        if node is None:
            return None
        if not isinstance(node, dict):
            raise ValueError("invalid protocol object")
        node = node.get(part)
    return node


def project_clinical_trial(raw_content: str) -> ClinicalTrialProjection:
    """Read one known Markdown appendix; ambiguous or invalid sources stay unknown.

    The returned JSON text is an exact source slice. Field paths are relative to
    the original study object, including its protocolSection member.
    """

    def unavailable(reason):
        return ClinicalTrialProjection(status="unavailable", reason=reason)

    if len(raw_content.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        return unavailable("document_too_large")
    sections = list(re.finditer(r"(?m)^##[ \t]+原始数据[ \t]*\r?$", raw_content))
    if len(sections) != 1:
        return unavailable("missing_or_ambiguous_raw_section")
    markers = list(re.finditer(r"(?m)^\*\*protocolSection\*\*:[ \t]*", raw_content))
    if len(markers) != 1 or markers[0].start() <= sections[0].end():
        return unavailable("missing_or_ambiguous_protocol_section")
    start = markers[0].end()
    while start < len(raw_content) and raw_content[start].isspace():
        start += 1
    try:
        protocol, length = json.JSONDecoder(
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        ).raw_decode(raw_content[start:])
        end = start + length
        # The stored formatter writes exactly one JSON value on this line.
        tail = raw_content[end:].split("\n", 1)[0]
        if tail.strip() or not isinstance(protocol, dict):
            return unavailable("invalid_protocol_json")
        values = {name: _value(protocol, path) for name, path in FIELD_PATHS.items()}
        # Source objects may contain more fields; retain list order and exact values.
        if values["interventions"] is not None:
            if not isinstance(values["interventions"], list):
                raise ValueError("invalid intervention array")
            values["interventions"] = [
                {"type": item.get("type"), "name": item.get("name")} for item in values["interventions"]
            ]
        for name in (
            "study_first_post_date",
            "last_update_post_date",
            "start_date",
            "primary_completion_date",
            "completion_date",
        ):
            if values[name] is not None:
                values[name] = {key: values[name].get(key) for key in ("date", "type")}
        facts = ClinicalTrialFacts.model_validate(values)
    except (ValueError, TypeError, AttributeError, RecursionError, ValidationError):
        return unavailable("invalid_protocol_fields_or_json")
    return ClinicalTrialProjection(
        status="available",
        text=raw_content[start:end],
        locator=ProtocolLocator(start=start, end=end),
        facts=facts,
        field_paths=dict(FIELD_PATHS),
    )
