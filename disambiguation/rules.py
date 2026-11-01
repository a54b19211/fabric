"""Identity rules: scoped unique identifiers can link; weaker evidence proposes."""

import re

from db.repository import lookup_external_id_candidates
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_AUTO_MATCHED, STATUS_CANDIDATE_MATCHED, STATUS_AMBIGUOUS,
)


# CAS identifies a compound, not a branded/dosed Drug. Gene symbols, approval
# numbers (which can cover multiple events), ICD codes and placeholder IDs do
# not identify entities at their respective object granularity.
STRONG_ID_FIELDS: dict[str, list[str]] = {
    "Company": ["stock_code"],
    "ClinicalTrial": ["trial_id"],
    "Compound": ["cas_number"],
    "Pathway": ["id"],
}

_EXCHANGES = {
    "NASDAQ": "XNAS", "XNAS": "XNAS", "NYSE": "XNYS", "XNYS": "XNYS",
    "SSE": "XSHG", "SH": "XSHG", "XSHG": "XSHG", "上交所": "XSHG",
    "SZSE": "XSHE", "SZ": "XSHE", "XSHE": "XSHE", "深交所": "XSHE",
    "HKEX": "XHKG", "HK": "XHKG", "XHKG": "XHKG", "港交所": "XHKG",
    "LSE": "XLON", "XLON": "XLON",
}


def _exchange(value) -> str | None:
    return _EXCHANGES.get(str(value or "").strip().upper())


def _valid_cas(value: str) -> bool:
    if not re.fullmatch(r"[1-9]\d{1,6}-\d{2}-\d", value):
        return False
    digits = value.replace("-", "")
    return sum(i * int(digit) for i, digit in enumerate(digits[-2::-1], 1)) % 10 == int(digits[-1])


def _valid_stock(value: str, exchange: str | None) -> bool:
    pattern = {
        "XNAS": r"[A-Z]{1,5}(?:[.-][A-Z])?", "XNYS": r"[A-Z]{1,5}(?:[.-][A-Z])?",
        "XSHG": r"[0-9]{6}", "XSHE": r"[0-9]{6}", "XHKG": r"[0-9]{4,5}",
        "XLON": r"[A-Z0-9]{1,5}\.?",
    }.get(exchange)
    return bool(pattern and re.fullmatch(pattern, value))


def _company_namespace_matches(row: dict, namespace: str) -> bool:
    exchanges = [(row.get(field) or {}).get("exchange")
                 for field in ("external_ids", "canonical_data")]
    exchanges = [value for value in exchanges if value and str(value).strip()]
    return bool(exchanges) and all(_exchange(value) == namespace for value in exchanges)


def _lookup(entity_type: str, field: str, value: str, session=None) -> list[dict]:
    rows = lookup_external_id_candidates(entity_type, field, value, session=session)
    # Duplicate rows for one dict_id do not represent multiple objects.
    return list({row["dict_id"]: row for row in rows}.values())


def _result(rows: list[dict], step: str, reason: str,
            *, auto_link: bool = False, selected_id: int | None = None,
            confidence: float = 0.85) -> DisambiguationResult | None:
    if not rows:
        return None
    candidates = [CandidateEntity(
        dict_id=row["dict_id"], name=row.get("canonical_name", ""),
        similarity_score=confidence, match_type="external_id" if auto_link else "rule",
        canonical_data=row.get("canonical_data") or {},
        external_ids=row.get("external_ids") or {},
    ) for row in rows]
    if selected_id is None and len(candidates) == 1:
        selected_id = candidates[0].dict_id
    selected = next((candidate for candidate in candidates if candidate.dict_id == selected_id), None)
    return DisambiguationResult(
        status=STATUS_AUTO_MATCHED if auto_link else (
            STATUS_CANDIDATE_MATCHED if len(candidates) == 1 else STATUS_AMBIGUOUS),
        dict_id=selected_id, confidence=confidence,
        matched_name=selected.name if selected else "", reason=reason,
        candidates=candidates, match_step=step, auto_link=auto_link,
    )


def match_strong_id(entity_type: str, entity_data: dict | None,
                    session=None) -> DisambiguationResult | None:
    """Only validated, namespace-correct, unique matches authorize a link."""
    if not entity_data:
        return None
    for field in STRONG_ID_FIELDS.get(entity_type, []):
        value = str(entity_data.get(field) or "").strip()
        namespace = None
        if entity_type == "ClinicalTrial" and re.fullmatch(r"NCT\d{8}", value):
            namespace = "clinicaltrials.NCT"
        elif entity_type == "Compound" and _valid_cas(value):
            namespace = "CAS"
        elif entity_type == "Pathway" and re.fullmatch(r"R-[A-Z]{3}-[1-9]\d*", value):
            namespace = "Reactome"
        elif entity_type == "Company":
            namespace = _exchange(entity_data.get("exchange"))
            if not _valid_stock(value, namespace):
                continue
        if not value or namespace is None:
            continue

        rows = _lookup(entity_type, field, value, session=session)
        eligible = rows
        if entity_type == "Company":
            eligible = [row for row in rows if _company_namespace_matches(row, namespace)]
        if len(eligible) == 1:
            return _result(
                rows, "step1_strong_id", f"unique scoped identifier: {namespace}:{value}",
                auto_link=True, selected_id=eligible[0]["dict_id"], confidence=0.99,
            )
        if rows:
            return _result(rows, "step1_strong_id",
                           f"identifier requires review: {namespace}:{value}; "
                           f"namespace-compatible matches={len(eligible)}")
    return None


def match_by_entity_rules(entity_name: str, entity_type: str,
                          entity_data: dict | None, session=None) -> DisambiguationResult | None:
    """Existing type-specific evidence remains a proposal unless a strong rule proves it."""
    if not entity_data:
        return None
    strong = match_strong_id(entity_type, entity_data, session=session)
    if strong:
        strong.match_step = {
            "Company": "step3_company_rule", "Compound": "step3_compound_cas",
            "ClinicalTrial": "step3_trial_rule", "Pathway": "step3_pathway_rule",
        }.get(entity_type, "step3_rule")
        return strong

    weak_fields = {
        "Company": [("stock_code", "step3_company_rule")],
        "Drug": [("cas_number", "step3_drug_cas"), ("id", "step3_drug_id")],
        "Target": [("gene_symbol", "step3_target_rule")],
        "ApprovalEvent": [("approval_number", "step3_approval_rule")],
    }
    rows_by_id = {}
    steps = []
    for field, step in weak_fields.get(entity_type, []):
        value = str(entity_data.get(field) or "").strip()
        if not value:
            continue
        rows = _lookup(entity_type, field, value, session=session)
        if rows:
            rows_by_id.update({row["dict_id"]: row for row in rows})
            steps.append(step)
    return _result(list(rows_by_id.values()), steps[0] if steps else "step3_rule",
                   "identifier evidence requires review; namespace or entity granularity is insufficient")
