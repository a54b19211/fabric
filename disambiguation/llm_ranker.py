"""
Step 5: 交叉编码器精排（BGE-Reranker）
Step 6: LLM 兜底判断（reranker 不确定时）
"""
import json
import math
import re
from dataclasses import replace

import requests
from loguru import logger

from config.settings import APP
from core_engine.cloud_api_client import qwen_chat
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_CANDIDATE_MATCHED, STATUS_NEW_ENTITY, STATUS_AMBIGUOUS,
)

# Existing bands only prioritize uncalibrated review proposals. They are
# not calibrated acceptance/non-match thresholds, and never authorize links.
SCORE_HIGH = 0.7
SCORE_MID = 0.5
SCORE_GAP = 0.15


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
    """Rank review candidates without treating scores as calibrated decisions."""
    if not candidates:
        return DisambiguationResult(
            status=STATUS_NEW_ENTITY, reason="Reranker: no candidates to rank",
            match_step="step5_reranker",
        )
    query = _build_rich_name(entity_name, entity_data)
    if source_text:
        query += " [ctx] " + source_text[:200]
    texts = [_build_rich_text(candidate) for candidate in candidates]
    trace = {
        "stage": "step5_reranker", "model_version": None,
        "input": {"query": query, "documents": texts,
                  "candidate_ids": [candidate.dict_id for candidate in candidates]},
        "output": None,
    }
    try:
        response = requests.post(_build_rerank_url(),
                                 json={"query": query, "documents": texts}, timeout=30)
        response.raise_for_status()
        data = response.json()
        trace["output"] = data
        if isinstance(data, dict):
            version = data.get("model_version") or data.get("model")
            trace["model_version"] = version if isinstance(version, str) and version else None
            results = data.get("results")
        else:
            results = data
        if not isinstance(results, list) or not results:
            raise ValueError("empty or invalid reranker results")
        scores = {}
        for item in results:
            index = item["index"]
            score = float(item["relevance_score"])
            if (type(index) is not int or index < 0 or index >= len(candidates)
                    or index in scores or not math.isfinite(score) or not 0 <= score <= 1):
                raise ValueError("invalid reranker candidate index or score")
            scores[index] = score
        if len(scores) != len(candidates):
            raise ValueError("incomplete reranker results")
        ranked = [replace(candidate, similarity_score=scores[index], match_type="reranker")
                  for index, candidate in enumerate(candidates)]
        ranked.sort(key=lambda candidate: candidate.similarity_score, reverse=True)
    except Exception as error:
        trace["error"] = str(error)
        logger.warning(f"[Reranker] Review ranking unavailable: {error}")
        return DisambiguationResult(
            status=STATUS_AMBIGUOUS, reason=f"Reranker call/output failed: {error}",
            candidates=candidates, match_step="step5_reranker_error",
            model_version=trace["model_version"], model_trace=[trace],
        )
    top = ranked[0]
    second = ranked[1].similarity_score if len(ranked) > 1 else 0.0
    preferred = top.similarity_score >= SCORE_HIGH and top.similarity_score - second >= SCORE_GAP
    return DisambiguationResult(
        status=STATUS_CANDIDATE_MATCHED if preferred else STATUS_AMBIGUOUS,
        dict_id=top.dict_id if preferred else None,
        confidence=top.similarity_score, matched_name=top.name if preferred else "",
        reason=f"Reranker proposal (uncalibrated): top1={top.similarity_score:.3f} top2={second:.3f}; requires review",
        candidates=ranked, match_step="step5_reranker",
        model_version=trace["model_version"], model_trace=[trace],
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
            "trial_id", "approval_number", "gene_symbol", "exchange", "id",
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
    显式启用后的复核提案，不执行链接或新建决定。
    模型只看有限候选，但返回结果始终保留完整候选集。
    """
    if not candidates:
        return DisambiguationResult(
            status=STATUS_AMBIGUOUS,
            confidence=0.0,
            reason="LLM review skipped: no candidates",
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

    # Keep the exact model input/output even when parsing or invocation fails.
    version = APP.LLM_MODEL_NAME or None
    trace = {
        "stage": "step6_llm", "model_version": version,
        "input": {"system_prompt": LLM_SYSTEM_PROMPT, "user_message": user_message,
                  "temperature": 0.0, "max_tokens": 300,
                  "candidate_ids": [candidate.dict_id for candidate in limited]},
        "output": None,
    }
    try:
        raw = qwen_chat(system_prompt=LLM_SYSTEM_PROMPT, user_message=user_message,
                        temperature=0.0, max_tokens=300)
        trace["output"] = raw
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = json.loads(cleaned)
        if not isinstance(result, dict):
            raise ValueError("LLM output must be an object")
        confidence = float(result.get("confidence", 0.0))
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("invalid LLM confidence")
        reason = str(result.get("reason", ""))
        match_key = result.get("match")
        matched = None
        if match_key != "none":
            if not isinstance(match_key, str) or not re.fullmatch(r"candidate_[0-9]+", match_key):
                raise ValueError(f"invalid LLM match key: {match_key}")
            index = int(match_key.split("_")[-1])
            if index >= len(limited):
                raise ValueError(f"LLM candidate out of range: {match_key}")
            matched = limited[index]
        trace["result"] = {
            "proposed_decision": "link_candidate" if matched else "no_match",
            "dict_id": matched.dict_id if matched else None,
            "confidence": confidence, "reason": reason,
        }
        return DisambiguationResult(
            status=STATUS_CANDIDATE_MATCHED if matched else STATUS_AMBIGUOUS,
            dict_id=matched.dict_id if matched else None, confidence=confidence,
            matched_name=matched.name if matched else "",
            reason=f"LLM proposal (uncalibrated): {reason}; requires review",
            candidates=candidates, match_step="step6_llm",
            model_version=version, model_trace=[trace],
        )
    except Exception as error:
        trace["error"] = str(error)
        logger.warning(f"[LLM] Review unavailable: {error}")
        return DisambiguationResult(
            status=STATUS_AMBIGUOUS, reason=f"LLM call/parse failed: {error}",
            candidates=candidates, match_step="step6_llm_error",
            model_version=version, model_trace=[trace],
        )
