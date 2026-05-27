"""
消歧引擎主编排器 —— 6 阶段流水线。

Step 0: 名称规范化 → 候选名称列表
Step 1: 强 ID 匹配（external_ids）
Step 2: 别名精确匹配（多候选名称）
Step 3: 实体类型规则匹配
Step 4: 向量召回（pgvector）
Step 5: LLM 精排（仅模糊场景，绝不 auto_matched）
"""

from loguru import logger

from db.repository import lookup_alias, search_similar_entities
from core_engine.cloud_api_client import generate_embedding, build_embedding_text
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_AUTO_MATCHED, STATUS_CANDIDATE_MATCHED,
    STATUS_NEW_ENTITY, STATUS_AMBIGUOUS,
)
from disambiguation.normalizer import generate_candidate_names
from disambiguation.rules import match_strong_id, match_by_entity_rules
from disambiguation.llm_ranker import rank_with_llm


# ── 向量阈值 ──
THRESHOLD_HIGH = 0.92    # top1 >= this AND gap >= GAP → candidate_matched
THRESHOLD_MID = 0.85     # top1 >= this → candidate_matched 或 ambiguous
GAP_THRESHOLD = 0.05     # top1 - top2 >= this 才是明确领先


def disambiguate(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None = None,
    source_text: str = "",
    source_site: str = "",
) -> DisambiguationResult:
    """
    完整 6 阶段消歧流水线，返回 DisambiguationResult。
    """
    entity_data = entity_data or {}

    # ── Step 0: 名称规范化 ────────────────────────────────────────
    candidate_names = generate_candidate_names(entity_name, entity_type, entity_data)

    # ── Step 1: 强 ID 匹配 ────────────────────────────────────────
    result = match_strong_id(entity_type, entity_data)
    if result:
        logger.info(f"[Disambiguation] Step1 hit: '{entity_name}' → dict_id={result.dict_id} ({result.reason})")
        return result

    # ── Step 2: 别名精确匹配（遍历候选名称）────────────────────────
    for idx, name in enumerate(candidate_names):
        dict_id = lookup_alias(name, entity_type)
        if dict_id:
            if idx == 0:
                confidence, match_type = 0.95, "exact_original"
            elif idx < 5:
                confidence, match_type = 0.90, "exact_alias"
            else:
                confidence, match_type = 0.85, "exact_normalized"

            reason = f"alias exact match: '{name}' (type={match_type})"
            logger.info(f"[Disambiguation] Step2 hit: '{entity_name}' → dict_id={dict_id} ({reason})")
            return DisambiguationResult(
                status=STATUS_AUTO_MATCHED,
                dict_id=dict_id,
                confidence=confidence,
                matched_name=name,
                reason=reason,
                match_step="step2_exact_alias",
            )

    # ── Step 3: 实体类型规则 ──────────────────────────────────────
    result = match_by_entity_rules(entity_name, entity_type, entity_data)
    if result and result.status == STATUS_AUTO_MATCHED:
        logger.info(f"[Disambiguation] Step3 hit: '{entity_name}' → dict_id={result.dict_id} ({result.reason})")
        return result
    step3_result = result  # 可能是 candidate_matched，暂存

    # ── Step 4: 向量召回 ──────────────────────────────────────────
    embed_text = build_embedding_text(entity_name, entity_type, entity_data)
    embedding = generate_embedding(embed_text)

    if embedding:
        similar = search_similar_entities(embedding, entity_type, top_k=5)

        if similar:
            top1_score = similar[0]["similarity_score"]
            top2_score = similar[1]["similarity_score"] if len(similar) > 1 else 0.0

            candidates = [
                CandidateEntity(
                    dict_id=s["dict_id"],
                    name=s["name"],
                    similarity_score=s["similarity_score"],
                    match_type="vector",
                )
                for s in similar
            ]

            # 高置信向量匹配
            if top1_score >= THRESHOLD_HIGH and (top1_score - top2_score) >= GAP_THRESHOLD:
                logger.info(f"[Disambiguation] Step4 high-conf: '{entity_name}' top1={top1_score:.3f}")
                return DisambiguationResult(
                    status=STATUS_CANDIDATE_MATCHED,
                    dict_id=candidates[0].dict_id,
                    confidence=top1_score,
                    matched_name=candidates[0].name,
                    reason=f"vector similarity: top1={top1_score:.3f} top2={top2_score:.3f}",
                    candidates=candidates,
                    match_step="step4_vector",
                )

            # 中等置信向量匹配
            if top1_score >= THRESHOLD_MID:
                gap_ok = (top1_score - top2_score) >= GAP_THRESHOLD
                logger.info(f"[Disambiguation] Step4 mid-conf: '{entity_name}' top1={top1_score:.3f} gap_ok={gap_ok}")
                return DisambiguationResult(
                    status=STATUS_CANDIDATE_MATCHED if gap_ok else STATUS_AMBIGUOUS,
                    dict_id=candidates[0].dict_id if gap_ok else None,
                    confidence=top1_score,
                    matched_name=candidates[0].name,
                    reason=f"vector similarity: top1={top1_score:.3f} top2={top2_score:.3f} (mid)",
                    candidates=candidates,
                    match_step="step4_vector",
                )

            # 低置信 → 尝试 LLM 精排
            logger.info(f"[Disambiguation] Step4 low-conf: '{entity_name}' top1={top1_score:.3f}")
            if source_text or entity_data:
                llm_result = rank_with_llm(
                    entity_name=entity_name,
                    entity_type=entity_type,
                    entity_data=entity_data,
                    source_text=source_text,
                    candidates=candidates,
                )
                logger.info(f"[Disambiguation] Step5 LLM: '{entity_name}' → {llm_result.status} ({llm_result.reason})")
                return llm_result

            return DisambiguationResult(
                status=STATUS_NEW_ENTITY,
                confidence=0.0,
                reason=f"vector top1={top1_score:.3f} below threshold, no LLM context",
                match_step="step4_vector",
            )
    else:
        logger.warning(f"[Disambiguation] Step4: embedding 生成失败 '{entity_name}'")

    # ── 无匹配，返回 step3 的候选结果或 new_entity ─────────────────
    if step3_result:
        return step3_result

    logger.info(f"[Disambiguation] No match: '{entity_name}' → new_entity")
    return DisambiguationResult(
        status=STATUS_NEW_ENTITY,
        confidence=0.0,
        reason="no match in any step",
        match_step="step_none",
    )


# ── 批量入口 ──

def disambiguate_entities(entities: list[dict]) -> list[dict]:
    """
    批量消歧入口，供 extraction_flow 调用。
    对每个 entity dict 原地设置 dict_id / confidence_score / confidence_reason，
    并将完整消歧结果存入 entity_data["_disambig"] 供前端展示。
    """
    for ent in entities:
        name = ent.get("entity_name", "")
        entity_type = ent.get("entity_type", "")
        entity_data = ent.get("entity_data", {})
        source_text = ent.get("source_text", "")
        source_site = ent.get("source_site", "")

        if not name or not entity_type:
            continue

        try:
            result = disambiguate(
                entity_name=name,
                entity_type=entity_type,
                entity_data=entity_data,
                source_text=source_text,
                source_site=source_site,
            )

            if result.status in (STATUS_AUTO_MATCHED, STATUS_CANDIDATE_MATCHED):
                ent["dict_id"] = result.dict_id
            else:
                ent["dict_id"] = None

            ent["confidence_score"] = result.confidence
            ent["confidence_reason"] = result.reason
            ent["_disambig"] = {
                "status": result.status,
                "candidates": [
                    {
                        "dict_id": c.dict_id,
                        "name": c.name,
                        "similarity_score": c.similarity_score,
                        "match_type": c.match_type,
                    }
                    for c in result.candidates
                ],
                "match_step": result.match_step,
            }

        except Exception as e:
            logger.error(f"[Disambiguation] Error for '{name}': {e}")
            ent["dict_id"] = None
            ent["confidence_score"] = 0.0
            ent["confidence_reason"] = f"disambiguation error: {e}"

    return entities
