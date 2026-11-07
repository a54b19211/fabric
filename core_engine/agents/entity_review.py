# -*- coding: utf-8 -*-
"""实体审核 — 三步固定 Pipeline。每步一次 LLM 调用，不走 ReAct 循环。"""

import json
import time

from langchain_openai import ChatOpenAI
from loguru import logger

from core_engine.agent_config import AgentConfig
from core_engine.agents.prompts import (
    ENTITY_STEP1_VALIDITY_PROMPT,
    ENTITY_STEP2_TYPE_PROMPT,
    ENTITY_STEP3_EVIDENCE_PROMPT,
)


def _parse_json_response(content: str) -> dict:
    """从 LLM 返回内容中解析 JSON。"""
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[entity_review] JSON 解析失败，原始返回: {content[:500]}")
        return {}


def _make_model(config: AgentConfig) -> ChatOpenAI:
    return ChatOpenAI(
        model=config.model_name,
        api_key=config.llm_api_key,
        base_url=config.llm_api_base,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        model_kwargs=config.model_kwargs,
    )


# ── Step 1: 有效性 ──────────────────────────────────────────────────────

def step1_validity(
    entity_name: str,
    entity_type: str,
    config: AgentConfig | None = None,
) -> dict:
    """判断实体名称是否为有效实体（非噪音词）。"""
    if config is None:
        config = AgentConfig()
    model = _make_model(config)
    t0 = time.time()

    prompt = ENTITY_STEP1_VALIDITY_PROMPT.format(
        entity_name=entity_name,
        entity_type=entity_type,
    )
    response = model.invoke(prompt)
    result = _parse_json_response(response.content)

    step_result = {
        "step_id": "validity",
        "step_name": "有效性",
        "status": "passed" if result.get("valid") else "failed",
        "input": {"entity_name": entity_name, "entity_type": entity_type},
        "output": result,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    logger.info(f"[entity_review] Step1 有效性: {entity_name} → {step_result['status']}")
    return step_result


# ── Step 2: 类型正确性 ──────────────────────────────────────────────────

def step2_type_check(
    entity_name: str,
    entity_type: str,
    config: AgentConfig | None = None,
) -> dict:
    """判断 entity_type 标注是否正确。"""
    if config is None:
        config = AgentConfig()
    model = _make_model(config)
    t0 = time.time()

    prompt = ENTITY_STEP2_TYPE_PROMPT.format(
        entity_name=entity_name,
        entity_type=entity_type,
    )
    response = model.invoke(prompt)
    result = _parse_json_response(response.content)

    step_result = {
        "step_id": "type_check",
        "step_name": "类型正确性",
        "status": "passed" if result.get("type_correct") else "failed",
        "input": {"entity_name": entity_name, "entity_type": entity_type},
        "output": result,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    logger.info(f"[entity_review] Step2 类型: {entity_name} ({entity_type}) → {step_result['status']}")
    return step_result


# ── Step 3: 原文依据 ────────────────────────────────────────────────────

def step3_source_evidence(
    entity_name: str,
    entity_type: str,
    entity_data: dict,
    source_text: str,
    config: AgentConfig | None = None,
) -> dict:
    """逐字段验证 entity_data 在 source_text 中是否有原文依据。"""
    if config is None:
        config = AgentConfig()
    model = _make_model(config)
    t0 = time.time()

    # 过滤内部字段，只验证业务字段
    visible_data = {k: v for k, v in (entity_data or {}).items() if not k.startswith("_")}
    if not visible_data:
        return {
            "step_id": "source_evidence",
            "step_name": "原文依据",
            "status": "passed",
            "input": {"entity_name": entity_name, "fields": {}},
            "output": {"fields": [], "overall": "full", "reason": "无业务字段需要验证"},
            "duration_ms": 0,
        }

    prompt = ENTITY_STEP3_EVIDENCE_PROMPT.format(
        entity_name=entity_name,
        entity_type=entity_type,
        entity_data=json.dumps(visible_data, ensure_ascii=False),
        source_text=source_text[:3000],
    )
    response = model.invoke(prompt)
    result = _parse_json_response(response.content)

    overall = result.get("overall", "partial")
    step_status = "failed" if overall == "none" else "passed"

    step_result = {
        "step_id": "source_evidence",
        "step_name": "原文依据",
        "status": step_status,
        "input": {"entity_name": entity_name, "fields": visible_data, "source_text": source_text[:500]},
        "output": result,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    logger.info(f"[entity_review] Step3 原文依据: {entity_name} → {overall}, fields={len(result.get('fields', []))}")
    return step_result


# ── Pipeline 入口 ───────────────────────────────────────────────────────

def run_entity_review_pipeline(
    entity: dict,
    config: AgentConfig | None = None,
) -> dict:
    """顺序执行三步 Pipeline。任一步失败即终止，后续步骤标记为 skipped。

    entity 字段：
        entity_name, entity_type, entity_data, source_text, source_site,
        confidence_score, confidence_reason

    Returns:
        { decision, reason, confidence, steps }
    """
    if config is None:
        config = AgentConfig()

    entity_name = entity.get("entity_name", "")
    entity_type = entity.get("entity_type", "")
    entity_data = entity.get("entity_data") or {}
    source_text = entity.get("source_text", "")

    steps: list[dict] = []
    reason_parts: list[str] = []

    # ── Step 1: 有效性 ──
    s1 = step1_validity(entity_name, entity_type, config)
    steps.append(s1)
    if s1["status"] == "failed":
        reason_parts.append(f"有效性不通过: {s1['output'].get('reason', '')}")
        return _build_result("rejected", reason_parts, s1["output"].get("confidence", 0.8), steps)

    reason_parts.append("有效性通过")
    s1_conf = s1["output"].get("confidence", 1.0)

    # ── Step 2: 类型正确性 ──
    s2 = step2_type_check(entity_name, entity_type, config)
    steps.append(s2)
    if s2["status"] == "failed":
        suggested = s2["output"].get("suggested_type", "")
        reason_parts.append(f"类型不正确: {s2['output'].get('reason', '')}" + (f" (建议: {suggested})" if suggested else ""))
        return _build_result("rejected", reason_parts, s2["output"].get("confidence", 0.8), steps)

    reason_parts.append(f"类型{entity_type}正确")
    s2_conf = s2["output"].get("confidence", 1.0)

    # ── Step 3: 原文依据 ──
    s3 = step3_source_evidence(entity_name, entity_type, entity_data, source_text, config)
    steps.append(s3)
    overall = s3["output"].get("overall", "partial")
    if overall == "none":
        reason_parts.append(f"原文依据不通过: {s3['output'].get('reason', '')}")
        return _build_result("rejected", reason_parts, s3["output"].get("confidence", 0.8), steps)
    elif overall == "partial":
        reason_parts.append(f"原文依据部分通过: {s3['output'].get('reason', '')}")
        s3_conf = 0.7
    else:
        reason_parts.append("原文依据全部通过")
        s3_conf = s3["output"].get("confidence", 1.0)

    # ── 全部通过 ──
    avg_confidence = round((s1_conf + s2_conf + s3_conf) / 3, 2)
    return _build_result("approved", reason_parts, avg_confidence, steps)


def _build_result(
    decision: str,
    reason_parts: list[str],
    confidence: float,
    steps: list[dict],
) -> dict:
    return {
        "decision": decision,
        "reason": "；".join(reason_parts),
        "confidence": confidence,
        "steps": steps,
    }
