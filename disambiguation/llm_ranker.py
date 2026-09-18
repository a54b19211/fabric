"""
Step 5: 交叉编码器精排（BGE-Reranker）
Step 6: LLM 兜底判断（reranker 不确定时）
"""
import requests
from loguru import logger

from config.settings import APP
from core_engine.cloud_api_client import qwen_chat
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_CANDIDATE_MATCHED, STATUS_NEW_ENTITY, STATUS_AMBIGUOUS,
)

SCORE_HIGH = 0.7       # 高于此阈值 → candidate_matched
SCORE_MID = 0.5        # 高于此阈值 → ambiguous（需人工审核）
SCORE_GAP = 0.15       # top1 - top2 需大于此差值才直接命中


def _build_rerank_url() -> str:
    base = APP.RERANK_API_BASE.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/rerank"


def _build_rich_name(entity_name: str, entity_data: dict | None) -> str:
    """构造多语言名称字符串，供 reranker query 和 text 使用。"""
    parts = [entity_name]
    if entity_data:
        name = entity_data.get("name", "").strip()
        if name and name != entity_name:
            parts.append(name)
        name_en = entity_data.get("name_en", "").strip()
        if name_en:
            parts.append(name_en)
        for alias in (entity_data.get("aliases") or []):
            a = str(alias).strip()
            if a and a not in parts:
                parts.append(a)
    return " | ".join(parts)


def _build_rich_text(candidate: CandidateEntity) -> str:
    """为每个候选构造多语言名称字符串（含 canonical_data 和 aliases）。"""
    parts = [candidate.name]
    canonical = candidate.canonical_data or {}
    cd_name = canonical.get("name", "").strip()
    if cd_name and cd_name != candidate.name:
        parts.append(cd_name)
    cd_name_en = canonical.get("name_en", "").strip()
    if cd_name_en:
        parts.append(cd_name_en)
    for alias in (canonical.get("aliases") or []):
        a = str(alias).strip()
        if a and a not in parts:
            parts.append(a)
    for alias in candidate.aliases:
        a = str(alias).strip()
        if a and a not in parts:
            parts.append(a)
    return " | ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Step 5: Reranker 精排
# ═══════════════════════════════════════════════════════════════════════════

def rank_with_reranker(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None,
    source_text: str,
    candidates: list[CandidateEntity],
) -> DisambiguationResult:
    """
    交叉编码器精排。
    用 BGE-Reranker 对 (query=实体多语言名, text=候选多语言名) 逐对打分。
    高分命中 → candidate_matched，中分 → ambiguous，低分 → new_entity。
    """
    if not candidates:
        return DisambiguationResult(
            status=STATUS_NEW_ENTITY,
            confidence=0.0,
            reason="Reranker: no candidates to rank",
            match_step="step5_reranker",
        )

    query = _build_rich_name(entity_name, entity_data)
    if source_text:
        query = query + " [ctx] " + source_text[:200]

    texts = [_build_rich_text(c) for c in candidates]

    try:
        resp = requests.post(
            _build_rerank_url(),
            json={"query": query, "documents": texts},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results = data
        else:
            results = data.get("results", [])
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

    for r in results:
        idx = r["index"]
        if idx < len(candidates):
            candidates[idx].similarity_score = r["relevance_score"]
            candidates[idx].match_type = "reranker"

    candidates.sort(key=lambda c: c.similarity_score, reverse=True)

    top1_score = candidates[0].similarity_score
    top2_score = candidates[1].similarity_score if len(candidates) > 1 else 0.0
    gap = top1_score - top2_score

    logger.info(
        f"[Reranker] '{entity_name}' top1={candidates[0].name} score={top1_score:.3f} "
        f"gap={gap:.3f}"
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


# ═══════════════════════════════════════════════════════════════════════════
# Step 6: LLM 兜底判断
# ═══════════════════════════════════════════════════════════════════════════

LLM_SYSTEM_PROMPT = (
    "你是医药实体消歧专家。判断待消歧实体与下列哪个候选是同一真实世界实体，"
    "或者都不是（新实体）。"
    "只回答一个 JSON 对象，不要添加任何额外文字。\n"
    '格式: {{"match": "candidate_0" | "candidate_1" | ... | "none", '
    '"confidence": 0.0-1.0, "reason": "判断依据"}}'
)

LLM_MAX_CANDIDATES = 5   # 单次会话最多包含的候选数


def _format_entity_fields(data: dict | None, indent: str = "  ") -> str:
    """将 entity_data / canonical_data 格式化为 LLM 可读的字段列表。"""
    if not data:
        return indent + "(无)"
    keys = ("name", "name_en", "aliases", "stock_code", "cas_number",
            "trial_id", "approval_number", "gene_symbol",
            "country", "primary_focus", "phase_cn")
    lines = []
    for k in keys:
        val = data.get(k)
        if val and str(val).strip():
            lines.append(f"{indent}{k}: {val}")
    return "\n".join(lines) if lines else indent + "(无)"


def rank_with_llm(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None,
    source_text: str,
    candidates: list[CandidateEntity],
) -> DisambiguationResult:
    """
    LLM 兜底判断。当 reranker 无法确定时调用。
    将所有候选放在一个会话里，让 LLM 一次性判断最佳匹配或新实体。
    """
    if not candidates:
        return DisambiguationResult(
            status=STATUS_NEW_ENTITY,
            confidence=0.0,
            reason="LLM: no candidates",
            match_step="step6_llm",
        )

    limited = candidates[:LLM_MAX_CANDIDATES]

    # ── 构造 prompt ──
    prompt_parts = [
        "[待消歧实体]",
        f"类型: {entity_type}",
        f"名称: {entity_name}",
        _format_entity_fields(entity_data),
    ]
    if source_text:
        prompt_parts.append(f"上下文: {source_text[:300]}")

    prompt_parts.append("")
    prompt_parts.append("[候选实体列表]")
    for i, c in enumerate(limited):
        prompt_parts.append(
            f"candidate_{i}: {c.name} (dict_id={c.dict_id}, "
            f"reranker_score={c.similarity_score:.3f})"
        )
        prompt_parts.append(_format_entity_fields(c.canonical_data))
        if c.aliases:
            prompt_parts.append(f"  所有别名: {', '.join(c.aliases[:10])}")
        prompt_parts.append("")

    prompt_parts.append(
        "请判断待消歧实体与哪个候选（candidate_0 / candidate_1 / ... / none）是同一实体。"
    )
    user_message = "\n".join(prompt_parts)

    # ── 调 LLM ──
    try:
        raw = qwen_chat(
            system_prompt=LLM_SYSTEM_PROMPT,
            user_message=user_message,
            temperature=0.0,
            max_tokens=300,
        )
        import json
        # 清理可能的 markdown 包裹
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        result = json.loads(raw)
    except Exception as e:
        logger.error(f"[LLM] 调用或解析失败: {e}")
        return DisambiguationResult(
            status=STATUS_AMBIGUOUS,
            confidence=candidates[0].similarity_score if candidates else 0.0,
            reason=f"LLM call/parse failed: {e}",
            candidates=limited,
            match_step="step6_llm_error",
        )

    match_key = result.get("match", "none")
    confidence = float(result.get("confidence", 0.0))
    reason = str(result.get("reason", ""))

    if match_key == "none":
        return DisambiguationResult(
            status=STATUS_NEW_ENTITY,
            confidence=confidence,
            reason=f"LLM: {reason}",
            candidates=limited,
            match_step="step6_llm",
        )

    # 解析 candidate_N → 下标
    try:
        idx = int(match_key.split("_")[-1])
        matched = limited[idx]
        return DisambiguationResult(
            status=STATUS_CANDIDATE_MATCHED,
            dict_id=matched.dict_id,
            confidence=confidence,
            matched_name=matched.name,
            reason=f"LLM: {reason}",
            candidates=limited,
            match_step="step6_llm",
        )
    except (IndexError, ValueError):
        return DisambiguationResult(
            status=STATUS_AMBIGUOUS,
            confidence=confidence,
            reason=f"LLM returned invalid match key: {match_key}",
            candidates=limited,
            match_step="step6_llm",
        )
