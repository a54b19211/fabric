"""
消歧引擎主编排器 —— 6 阶段流水线。

Step 0: 名称规范化 → 候选名称列表
Step 1: 强 ID 匹配（external_ids）
Step 2: 别名精确匹配（多候选名称）
Step 3: 实体类型规则匹配
Step 4: 向量召回（pgvector）—— 仅做候选集，分数不参与决策
Step 5: Reranker 精排（cross-encoder 语义打分）
Step 6: LLM 兜底判断（reranker 无法确定时）
"""

from loguru import logger

from db.repository import lookup_alias, search_similar_entities
from core_engine.cloud_api_client import generate_embedding
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_AUTO_MATCHED, STATUS_CANDIDATE_MATCHED,
    STATUS_NEW_ENTITY, STATUS_AMBIGUOUS,
)
from disambiguation.normalizer import generate_candidate_names, normalize_for_alignment
from disambiguation.rules import match_strong_id, match_by_entity_rules
from disambiguation.llm_ranker import rank_with_reranker, rank_with_llm


def disambiguate(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None = None,
    source_text: str = "",
    source_site: str = "",
    session=None,
) -> DisambiguationResult:
    """
    完整 6 阶段消歧流水线，返回 DisambiguationResult。
    session 可选：传入则复用，不传则各步自开（向后兼容）。
    """
    entity_data = entity_data or {}

    # ── Step 0: 名称规范化 ────────────────────────────────────────
    candidate_names = generate_candidate_names(entity_name, entity_type, entity_data)

    # ── Step 0.5: 对齐导向规范化（4.1，仅作向量召回辅助键）──────────
    normalized_name = normalize_for_alignment(entity_name, entity_type)
    if normalized_name and normalized_name != entity_name:
        entity_data["normalized_name"] = normalized_name

    # ── Step 1: 强 ID 匹配 ────────────────────────────────────────
    result = match_strong_id(entity_type, entity_data, session=session)
    if result:
        logger.info(f"[Disambiguation] Step1 hit: '{entity_name}' → dict_id={result.dict_id} ({result.reason})")
        return result

    # ── Step 2: 别名精确匹配（遍历候选名称）────────────────────────
    for idx, name in enumerate(candidate_names):
        dict_id = lookup_alias(name, entity_type, session=session)
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
    result = match_by_entity_rules(entity_name, entity_type, entity_data, session=session)
    if result and result.status == STATUS_AUTO_MATCHED:
        logger.info(f"[Disambiguation] Step3 hit: '{entity_name}' → dict_id={result.dict_id} ({result.reason})")
        return result
    step3_result = result  # 可能是 candidate_matched，暂存

    # ── Step 4: 向量召回（仅做候选集，分数不参与决策）──────────────
    vector_candidates: list[CandidateEntity] = []
    seen_ids: set[int] = set()

    def _recall_with_name(name: str, match_type: str = "vector") -> None:
        if not name or not name.strip():
            return
        emb = generate_embedding(name)
        if not emb:
            return
        similar = search_similar_entities(emb, entity_type, top_k=10, session=session)
        if not similar:
            return
        for s in similar:
            if s["dict_id"] not in seen_ids:
                seen_ids.add(s["dict_id"])
                vector_candidates.append(
                    CandidateEntity(
                        dict_id=s["dict_id"],
                        name=s["name"],
                        similarity_score=s["similarity_score"],
                        match_type=match_type,
                        canonical_data=s.get("canonical_data", {}),
                        aliases=s.get("aliases", []),
                    )
                )

    _recall_with_name(entity_name)
    name_en = (entity_data or {}).get("name_en", "").strip()
    if name_en and name_en.lower() != entity_name.lower():
        _recall_with_name(name_en, match_type="vector_en")

    if normalized_name and normalized_name.lower() != entity_name.lower() \
            and normalized_name.lower() != name_en.lower():
        _recall_with_name(normalized_name, match_type="vector_normalized")

    if vector_candidates:
        logger.info(
            f"[Disambiguation] Step4 recall: '{entity_name}' → "
            f"{len(vector_candidates)} candidates"
        )
    else:
        logger.warning(f"[Disambiguation] Step4: embedding 生成失败 '{entity_name}'")

    # ── Step 5: Reranker 精排 ──────────────────────────────────────
    all_candidates = vector_candidates
    if step3_result and step3_result.candidates:
        # 合并 step3 规则匹配的候选（去重 by dict_id）
        seen_ids = {c.dict_id for c in all_candidates}
        for c in step3_result.candidates:
            if c.dict_id not in seen_ids:
                all_candidates.append(c)
                seen_ids.add(c.dict_id)

    if all_candidates:
        reranker_result = rank_with_reranker(
            entity_name=entity_name,
            entity_type=entity_type,
            entity_data=entity_data,
            source_text=source_text,
            candidates=all_candidates,
        )
        logger.info(
            f"[Disambiguation] Step5 reranker: '{entity_name}' → "
            f"{reranker_result.status} ({reranker_result.reason})"
        )

        # Reranker 明确命中 → 直接返回
        if reranker_result.status == STATUS_CANDIDATE_MATCHED:
            return reranker_result

        # Reranker 不确定 → 暂禁用 Step 6 LLM 兜底，直接返回 ambiguous
        # （决策：Step6 后续降级为分级路由的中置信度复核，避免全量 LLM 兜底的延迟。
        #   注：原注释「拉长锁持有时间」是过时认知，消歧实际在锁外执行，真实代价是延迟。）
        if reranker_result.status == STATUS_AMBIGUOUS:
            # llm_result = rank_with_llm(
            #     entity_name=entity_name,
            #     entity_type=entity_type,
            #     entity_data=entity_data,
            #     source_text=source_text,
            #     candidates=reranker_result.candidates or all_candidates,
            # )
            # logger.info(
            #     f"[Disambiguation] Step6 LLM: '{entity_name}' → "
            #     f"{llm_result.status} ({llm_result.reason})"
            # )
            # return llm_result
            logger.info(f"[Disambiguation] Step6 LLM skipped: '{entity_name}' → ambiguous")
            return reranker_result

        # Reranker 返回 new_entity（低分）
        return reranker_result

    # ── 无召回候选 → 试试 step3 规则结果 → 否则 new_entity ─────────
    if step3_result:
        logger.info(f"[Disambiguation] Step3 fallback: '{entity_name}' → {step3_result.status}")
        return step3_result

    logger.info(f"[Disambiguation] No match: '{entity_name}' → new_entity")
    return DisambiguationResult(
        status=STATUS_NEW_ENTITY,
        confidence=0.0,
        reason="no match in any step",
        match_step="step_none",
    )


# ── 批量入口 ──

def disambiguate_entities(entities: list[dict], session=None) -> list[dict]:
    """
    批量消歧入口，供 extraction_flow 调用。
    对每个 entity dict 原地设置 dict_id / confidence_score / confidence_reason，
    并将完整消歧结果存入 entity_data["_disambig"] 供前端展示。
    session 可选：传入则复用，不传则各步自开（向后兼容）。
    """
    for ent in entities:
        name = ent.get("entity_name", "")
        entity_type = ent.get("entity_type", "")
        entity_data = ent.get("entity_data") or {}
        ent["entity_data"] = entity_data
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
                session=session,
            )

            # Step 2 别名精确匹配 + Step 1 强 ID 匹配（确定性唯一 ID）才设定 dict_id
            # Step 3/5 的匹配结果只记入 _disambig.candidates，不直接关联
            if result.match_step in ("step2_exact_alias", "step1_strong_id"):
                ent["dict_id"] = result.dict_id
                logger.info(
                    f"[Disambiguation] dict_id SET: '{name}' type={entity_type} "
                    f"→ dict_id={result.dict_id} step={result.match_step} "
                    f"status={result.status}"
                )
            else:
                ent["dict_id"] = None
                logger.info(
                    f"[Disambiguation] dict_id NONE: '{name}' type={entity_type} "
                    f"step={result.match_step} status={result.status} "
                    f"reason={result.reason}"
                )

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
