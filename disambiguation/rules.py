"""
强 ID 匹配（Step 1）和实体类型规则匹配（Step 3）。

Step 1: 通过 external_ids JSONB 查找，命中直接 auto_matched (0.99)
Step 3: 实体类型特定规则，按类型决定 auto_matched / candidate_matched
"""

from db.repository import lookup_external_id
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_AUTO_MATCHED, STATUS_CANDIDATE_MATCHED,
)


# ── Step 1: 各实体类型的强 ID 字段 ──

STRONG_ID_FIELDS: dict[str, list[str]] = {
    "Company":       ["id", "stock_code"],
    "Drug":          ["id", "cas_number"],
    "Target":        ["id", "gene_symbol"],
    "ClinicalTrial": ["trial_id"],
    "ApprovalEvent": ["approval_number"],
    "Compound":      ["cas_number"],
    "Policy":        ["doc_number"],
    "Pathway":       ["id"],
    "Indication":    ["id", "icd_code"],
}


def match_strong_id(
    entity_type: str,
    entity_data: dict | None,
    session=None,
) -> DisambiguationResult | None:
    """
    Step 1: 用外部 ID 匹配 entity_dict.external_ids。
    命中 → auto_matched (0.99)；未命中或无 ID → None。
    """
    if not entity_data:
        return None

    fields = STRONG_ID_FIELDS.get(entity_type, [])
    for field in fields:
        value = entity_data.get(field)
        if not value or not str(value).strip():
            continue

        result = lookup_external_id(
            entity_type=entity_type,
            external_key=field,
            external_value=str(value).strip(),
            session=session,
        )
        if result:
            return DisambiguationResult(
                status=STATUS_AUTO_MATCHED,
                dict_id=result["dict_id"],
                confidence=0.99,
                matched_name=result.get("canonical_name", ""),
                reason=f"external_id matched: {field}={value}",
                match_step="step1_strong_id",
            )

    return None


# ── Step 3: 实体类型规则 ──

def match_by_entity_rules(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None,
    session=None,
) -> DisambiguationResult | None:
    """Step 3: 实体类型特定规则匹配。"""
    if not entity_data:
        return None

    if entity_type == "Company":
        return _match_company(entity_data, session=session)
    if entity_type == "Drug":
        return _match_drug(entity_data, session=session)
    if entity_type == "Target":
        return _match_target(entity_data, session=session)

    return None


def _match_company(entity_data: dict, session=None) -> DisambiguationResult | None:
    """Company: stock_code + exchange 精确匹配 → auto_matched (0.95)。"""
    stock = str(entity_data.get("stock_code", "")).strip()
    exchange = str(entity_data.get("exchange", "")).strip()
    if stock and exchange:
        result = lookup_external_id("Company", "stock_code", stock, session=session)
        if result:
            return DisambiguationResult(
                status=STATUS_AUTO_MATCHED,
                dict_id=result["dict_id"],
                confidence=0.95,
                matched_name=result.get("canonical_name", ""),
                reason=f"company stock_code+exchange: {stock}@{exchange}",
                match_step="step3_company_rule",
            )
    return None


def _match_drug(entity_data: dict, session=None) -> DisambiguationResult | None:
    """Drug: CAS 或 id（批准文号）匹配 → auto_matched (0.95)。"""
    cas = str(entity_data.get("cas_number", "")).strip()
    if cas:
        result = lookup_external_id("Drug", "cas_number", cas, session=session)
        if result:
            return DisambiguationResult(
                status=STATUS_AUTO_MATCHED,
                dict_id=result["dict_id"],
                confidence=0.95,
                matched_name=result.get("canonical_name", ""),
                reason=f"drug CAS: {cas}",
                match_step="step3_drug_cas",
            )

    drug_id = str(entity_data.get("id", "")).strip()
    if drug_id:
        result = lookup_external_id("Drug", "id", drug_id, session=session)
        if result:
            return DisambiguationResult(
                status=STATUS_AUTO_MATCHED,
                dict_id=result["dict_id"],
                confidence=0.95,
                matched_name=result.get("canonical_name", ""),
                reason=f"drug ID: {drug_id}",
                match_step="step3_drug_id",
            )
    return None


def _match_target(entity_data: dict, session=None) -> DisambiguationResult | None:
    """Target: gene_symbol 匹配 → candidate_matched (0.85)，不自动落锤。"""
    gene = str(entity_data.get("gene_symbol", "")).strip()
    if gene:
        result = lookup_external_id("Target", "gene_symbol", gene, session=session)
        if result:
            return DisambiguationResult(
                status=STATUS_CANDIDATE_MATCHED,
                dict_id=result["dict_id"],
                confidence=0.85,
                matched_name=result.get("canonical_name", ""),
                reason=f"target gene_symbol: {gene}",
                match_step="step3_target_rule",
            )
    return None
