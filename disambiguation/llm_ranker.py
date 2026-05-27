"""
LLM 精排（Step 5）——仅用于模糊场景（ambiguous / 低置信候选）。
LLM 结果只给审核建议，绝不产生 auto_matched。
"""

import json
from loguru import logger
from openai import OpenAI

from config.settings import APP
from disambiguation.models import (
    DisambiguationResult, CandidateEntity,
    STATUS_CANDIDATE_MATCHED, STATUS_NEW_ENTITY, STATUS_AMBIGUOUS,
)


RANKING_PROMPT = """你是医药知识图谱实体消歧专家。
判断一个从文档中抽取的实体，是否与候选列表中的某个实体是同一个真实世界实体。

原则：
1. 仅基于提供的字段信息判断，不要引入外部知识。
2. 如果证据不足，选择 "unsure"。
3. 同一实体的不同名称（全称/简称/英文名/别名）应判为 same_entity。
4. 语义相似但不是同一实体（如 PD-1 vs PD-L1）应判为 different_entity。

输出 JSON：
{
  "decision": "same_entity | different_entity | unsure",
  "matched_dict_id": 123,
  "confidence": 0.85,
  "reason": "简短中文理由，不超过50字"
}
"""


def rank_with_llm(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None,
    source_text: str,
    candidates: list[CandidateEntity],
) -> DisambiguationResult:
    """
    Step 5: LLM 精排候选实体。仅用于模糊场景，LLM 绝不产生 auto_matched。
    """
    if not candidates:
        return DisambiguationResult(
            status=STATUS_NEW_ENTITY,
            confidence=0.0,
            reason="LLM: no candidates to rank",
            match_step="step5_llm",
        )

    extracted_info = {
        "entity_type": entity_type,
        "name": entity_name,
        "entity_data": entity_data or {},
        "source_text": source_text[:300],
    }
    candidates_info = []
    for c in candidates[:5]:
        candidates_info.append({
            "dict_id": c.dict_id,
            "name": c.name,
            "aliases": c.aliases[:10],
            "canonical_data_snippet": {
                k: v for k, v in c.canonical_data.items()
                if k in ("name", "name_en", "cas_number", "stock_code",
                         "gene_symbol", "trial_id", "approval_number",
                         "doc_number", "entity_type")
            },
        })

    prompt = json.dumps({
        "extracted_entity": extracted_info,
        "candidates": candidates_info,
    }, ensure_ascii=False, indent=2)

    try:
        client = OpenAI(api_key=APP.RERANK_API_KEY, base_url=APP.RERANK_API_BASE)
        response = client.chat.completions.create(
            model=APP.RERANK_MODEL_NAME,
            messages=[
                {"role": "system", "content": RANKING_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)

        decision = result.get("decision", "unsure")
        matched_id = result.get("matched_dict_id")
        confidence = float(result.get("confidence", 0.5))
        reason = result.get("reason", "")

        # LLM 绝不产生 auto_matched — 最高 candidate_matched
        if decision == "same_entity" and matched_id and confidence >= 0.9:
            return DisambiguationResult(
                status=STATUS_CANDIDATE_MATCHED,
                dict_id=matched_id,
                confidence=confidence,
                reason=f"LLM: {reason}",
                candidates=candidates,
                match_step="step5_llm",
            )
        elif decision == "unsure":
            return DisambiguationResult(
                status=STATUS_AMBIGUOUS,
                confidence=confidence,
                reason=f"LLM unsure: {reason}",
                candidates=candidates,
                match_step="step5_llm",
            )
        else:
            return DisambiguationResult(
                status=STATUS_NEW_ENTITY,
                confidence=0.0,
                reason=f"LLM: {reason}",
                candidates=candidates,
                match_step="step5_llm",
            )

    except Exception as e:
        logger.error(f"[LLM Ranker] 调用失败: {e}")
        return DisambiguationResult(
            status=STATUS_AMBIGUOUS,
            confidence=0.0,
            reason=f"LLM call failed: {e}",
            candidates=candidates,
            match_step="step5_llm_error",
        )
