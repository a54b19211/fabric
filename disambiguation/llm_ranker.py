"""
交叉编码器精排（Step 5）——用于模糊场景（向量召回低置信 / 多候选接近）。
通过 BGE-Reranker 对每个候选做 query-candidate 打分，不依赖 LLM 聊天。
"""

import requests
from loguru import logger

from config.settings import APP
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_CANDIDATE_MATCHED, STATUS_NEW_ENTITY, STATUS_AMBIGUOUS,
)

SCORE_HIGH = 0.7       # 高于此阈值 → candidate_matched
SCORE_MID = 0.5        # 高于此阈值 → ambiguous（不确定，需人工审核）
SCORE_GAP = 0.15       # top1 - top2 需大于此差值才直接命中


def _build_rerank_url() -> str:
    base = APP.RERANK_API_BASE.rstrip("/")
    # 去掉 /v1 后缀（TEI rerank 端点直接在根路径）
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/rerank"


def rank_with_reranker(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None,
    source_text: str,
    candidates: list[CandidateEntity],
) -> DisambiguationResult:
    """
    Step 5: 交叉编码器精排。

    用 BGE-Reranker 对 (query=实体名, text=候选名) 逐对打分，
    高分命中 → candidate_matched，中分 → ambiguous，低分 → new_entity。
    """
    if not candidates:
        return DisambiguationResult(
            status=STATUS_NEW_ENTITY,
            confidence=0.0,
            reason="Reranker: no candidates to rank",
            match_step="step5_reranker",
        )

    # 构造 query：优先带上 source_text 上下文
    query_parts = [f"[{entity_type}] {entity_name}"]
    if source_text:
        query_parts.append(source_text[:200])
    query = " ".join(query_parts)

    texts = [c.name for c in candidates]

    try:
        resp = requests.post(
            _build_rerank_url(),
            json={"query": query, "texts": texts},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        logger.error(f"[Reranker] 调用失败: {e}")
        return DisambiguationResult(
            status=STATUS_AMBIGUOUS,
            confidence=0.0,
            reason=f"Reranker call failed: {e}",
            candidates=candidates,
            match_step="step5_reranker_error",
        )

    if not results:
        return DisambiguationResult(
            status=STATUS_NEW_ENTITY,
            confidence=0.0,
            reason="Reranker: empty results",
            match_step="step5_reranker",
        )

    # 按 index 给 candidates 附加分数
    for r in results:
        idx = r["index"]
        if idx < len(candidates):
            candidates[idx].similarity_score = r["relevance_score"]

    # 按分数降序排
    candidates.sort(key=lambda c: c.similarity_score, reverse=True)

    top1_score = candidates[0].similarity_score
    top2_score = candidates[1].similarity_score if len(candidates) > 1 else 0.0
    gap = top1_score - top2_score

    logger.info(
        f"[Reranker] '{entity_name}' top1={candidates[0].name} score={top1_score:.3f} "
        f"top2={candidates[1].name if len(candidates)>1 else 'N/A'} score={top2_score:.3f} gap={gap:.3f}"
    )

    if top1_score >= SCORE_HIGH and gap >= SCORE_GAP:
        return DisambiguationResult(
            status=STATUS_CANDIDATE_MATCHED,
            dict_id=candidates[0].dict_id,
            confidence=top1_score,
            matched_name=candidates[0].name,
            reason=f"Reranker: top1={top1_score:.3f} top2={top2_score:.3f}",
            candidates=candidates,
            match_step="step5_reranker",
        )
    elif top1_score >= SCORE_MID:
        return DisambiguationResult(
            status=STATUS_AMBIGUOUS,
            confidence=top1_score,
            reason=f"Reranker: top1={top1_score:.3f} below high threshold",
            candidates=candidates,
            match_step="step5_reranker",
        )
    else:
        return DisambiguationResult(
            status=STATUS_NEW_ENTITY,
            confidence=0.0,
            reason=f"Reranker: top1={top1_score:.3f} below mid threshold",
            candidates=candidates,
            match_step="step5_reranker",
        )


# 保持原函数名兼容 engine.py 的调用
rank_with_llm = rank_with_reranker
