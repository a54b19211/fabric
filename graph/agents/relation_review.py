# -*- coding: utf-8 -*-
"""关系审核 — 三步固定 Pipeline。Step1 查 DB，Step2-3 LLM 调用。"""

import json
import time

from langchain_openai import ChatOpenAI
from loguru import logger

from graph.agents.config import AgentConfig
from graph.agents.prompts import (
    RELATION_STEP2_TYPE_PROMPT,
    RELATION_STEP3_EVIDENCE_PROMPT,
)


def _parse_json_response(content: str) -> dict:
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[relation_review] JSON 解析失败: {content[:500]}")
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


# ── Step 1: 端点存在性 ──────────────────────────────────────────────────

def step1_endpoint_check(
    from_entity_name: str,
    to_entity_name: str,
) -> dict:
    """检查关系两端实体是否存在（名称非空即认为已加载成功）。"""
    t0 = time.time()
    from_ok = bool(from_entity_name and from_entity_name.strip())
    to_ok = bool(to_entity_name and to_entity_name.strip())
    passed = from_ok and to_ok

    missing = []
    if not from_ok:
        missing.append("起点实体")
    if not to_ok:
        missing.append("终点实体")

    step_result = {
        "step_id": "endpoint_check",
        "step_name": "端点存在性",
        "status": "passed" if passed else "failed",
        "input": {"from_entity": from_entity_name, "to_entity": to_entity_name},
        "output": {
            "both_exist": passed,
            "missing": missing,
            "reason": "两端实体均存在" if passed else f"{'、'.join(missing)}不存在",
        },
        "duration_ms": int((time.time() - t0) * 1000),
    }
    logger.info(f"[relation_review] Step1 端点: {from_entity_name} → {to_entity_name} → {step_result['status']}")
    return step_result


# ── Step 2: 关系类型合法性 ──────────────────────────────────────────────

def step2_type_validity(
    relation_type: str,
    from_entity_name: str,
    from_entity_type: str,
    to_entity_name: str,
    to_entity_type: str,
    config: AgentConfig | None = None,
) -> dict:
    """LLM 判断关系类型对该实体类型对是否合法。"""
    if config is None:
        config = AgentConfig()
    model = _make_model(config)
    t0 = time.time()

    prompt = RELATION_STEP2_TYPE_PROMPT.format(
        relation_type=relation_type,
        from_entity_name=from_entity_name,
        from_entity_type=from_entity_type,
        to_entity_name=to_entity_name,
        to_entity_type=to_entity_type,
    )
    response = model.invoke(prompt)
    result = _parse_json_response(response.content)

    step_result = {
        "step_id": "type_validity",
        "step_name": "关系类型合法性",
        "status": "passed" if result.get("type_valid") else "failed",
        "input": {
            "relation_type": relation_type,
            "from": f"{from_entity_name}({from_entity_type})",
            "to": f"{to_entity_name}({to_entity_type})",
        },
        "output": result,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    logger.info(f"[relation_review] Step2 类型: {relation_type} → {step_result['status']}")
    return step_result


# ── Step 3: 原文依据 ────────────────────────────────────────────────────

def step3_source_evidence(
    relation_type: str,
    from_entity_name: str,
    to_entity_name: str,
    relation_props: dict,
    source_text: str,
    config: AgentConfig | None = None,
) -> dict:
    """LLM 验证关系在原文中是否有依据。"""
    if config is None:
        config = AgentConfig()
    model = _make_model(config)
    t0 = time.time()

    prompt = RELATION_STEP3_EVIDENCE_PROMPT.format(
        relation_type=relation_type,
        from_entity_name=from_entity_name,
        from_entity_type="",
        to_entity_name=to_entity_name,
        to_entity_type="",
        relation_props=json.dumps(relation_props or {}, ensure_ascii=False),
        source_text=source_text[:3000],
    )
    response = model.invoke(prompt)
    result = _parse_json_response(response.content)

    step_result = {
        "step_id": "source_evidence",
        "step_name": "原文依据",
        "status": "passed" if result.get("has_evidence") else "failed",
        "input": {
            "relation_type": relation_type,
            "from": from_entity_name,
            "to": to_entity_name,
            "source_text": source_text[:500],
        },
        "output": result,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    logger.info(f"[relation_review] Step3 原文依据: {from_entity_name} → {to_entity_name} → {step_result['status']}")
    return step_result


# ── Pipeline 入口 ───────────────────────────────────────────────────────

def run_relation_review_pipeline(
    rel: dict,
    config: AgentConfig | None = None,
) -> dict:
    """顺序执行三步 Pipeline。

    rel 字段：
        relation_type, from_entity_name, from_entity_type,
        to_entity_name, to_entity_type, relation_props, source_text

    Returns:
        { decision, reason, confidence, steps }
    """
    if config is None:
        config = AgentConfig()

    relation_type = rel.get("relation_type", "")
    from_name = rel.get("from_entity_name", "")
    from_type = rel.get("from_entity_type", "")
    to_name = rel.get("to_entity_name", "")
    to_type = rel.get("to_entity_type", "")
    relation_props = rel.get("relation_props") or {}
    source_text = rel.get("source_text", "")

    steps: list[dict] = []
    reason_parts: list[str] = []

    # ── Step 1: 端点存在性 ──
    s1 = step1_endpoint_check(from_name, to_name)
    steps.append(s1)
    if s1["status"] == "failed":
        reason_parts.append(f"端点不存在: {s1['output'].get('reason', '')}")
        return _build_result("rejected", reason_parts, 1.0, steps)
    reason_parts.append("两端实体均存在")

    # ── Step 2: 关系类型合法性 ──
    s2 = step2_type_validity(relation_type, from_name, from_type, to_name, to_type, config)
    steps.append(s2)
    if s2["status"] == "failed":
        suggested = s2["output"].get("suggested_type", "")
        reason_parts.append(f"关系类型不合法: {s2['output'].get('reason', '')}" + (f" (建议: {suggested})" if suggested else ""))
        return _build_result("rejected", reason_parts, s2["output"].get("confidence", 0.8), steps)
    reason_parts.append(f"关系类型{relation_type}合法")
    s2_conf = s2["output"].get("confidence", 1.0)

    # ── Step 3: 原文依据 ──
    s3 = step3_source_evidence(relation_type, from_name, to_name, relation_props, source_text, config)
    steps.append(s3)
    if s3["status"] == "failed":
        reason_parts.append(f"原文无依据: {s3['output'].get('reason', '')}")
        return _build_result("rejected", reason_parts, s3["output"].get("confidence", 0.8), steps)
    reason_parts.append("原文有依据")
    s3_conf = s3["output"].get("confidence", 1.0)

    # ── 全部通过 ──
    avg_confidence = round((s2_conf + s3_conf) / 2, 2)
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
