# -*- coding: utf-8 -*-
"""Agent 决策工具 — 审批/拒绝/冲突裁决/提交最终判断。"""

from sqlalchemy import false
import json
import math

from langchain_core.tools import tool
from loguru import logger

from db.repository import (
    approve_entity_with_alias_writeback,
    reject_entity as _reject_entity,
    approve_relation as _approve_relation,
    reject_relation as _reject_relation,
)
from db.session import get_session
from db.models import ExtractedEntity, EntityRelation


@tool
def approve_entity(entity_id: int, reviewer: str = "auto", note: str = "", dict_id: int | None = None) -> str:
    """批准实体通过审核（含 alias 回写 + canonical_data 合并 + embedding 生成）。

    Args:
        entity_id: ExtractedEntity 主键
        reviewer: 审核人标识，Agent 调用时固定为 "auto"
        note: 审核备注
        dict_id: 可选——关联到已有 entity_dict，为 None 则创建新的 entity_dict

    Returns:
        操作结果描述
    """
    try:
        with get_session() as session:
            row = session.query(ExtractedEntity).filter(
                ExtractedEntity.id == entity_id,
                ExtractedEntity.is_deleted == false(),
            ).first()
            if row is None:
                return f"错误: 实体 id={entity_id} 不存在"
            if row.review_status != "pending":
                return f"错误: 实体 id={entity_id} 状态为 {row.review_status}，不可重复审核"

        approve_entity_with_alias_writeback(
            entity_id, reviewer=reviewer, note=note,
            dict_id=dict_id, force_new=(dict_id is None),
        )
        logger.info(f"[AgentTool] 已批准实体: id={entity_id}, dict_id={dict_id}")
        return f"已批准: 实体 id={entity_id}" + (f" → dict_id={dict_id}" if dict_id else "（新建 entity_dict）")
    except Exception as e:
        logger.error(f"[AgentTool] approve_entity 失败: {e}")
        return f"批准失败: {str(e)}"


@tool
def reject_entity(entity_id: int, reviewer: str = "auto", note: str = "") -> str:
    """拒绝实体。会自动级联拒绝涉及该实体的待审核关系。

    Args:
        entity_id: ExtractedEntity 主键
        reviewer: 审核人标识
        note: 拒绝原因

    Returns:
        操作结果描述
    """
    try:
        with get_session() as session:
            row = session.query(ExtractedEntity).filter(
                ExtractedEntity.id == entity_id,
                ExtractedEntity.is_deleted == false(),
            ).first()
            if row is None:
                return f"错误: 实体 id={entity_id} 不存在"
            if row.review_status != "pending":
                return f"错误: 实体 id={entity_id} 状态为 {row.review_status}，不可重复审核"

        _reject_entity(entity_id, reviewer=reviewer, note=note)
        logger.info(f"[AgentTool] 已拒绝实体: id={entity_id}")
        return f"已拒绝: 实体 id={entity_id}（关联关系已级联拒绝）"
    except Exception as e:
        logger.error(f"[AgentTool] reject_entity 失败: {e}")
        return f"拒绝失败: {str(e)}"


@tool
def approve_relation(relation_id: int, reviewer: str = "auto", note: str = "") -> str:
    """批准关系通过审核。前提是两端实体均已被批准。

    Args:
        relation_id: EntityRelation 主键
        reviewer: 审核人标识
        note: 审核备注

    Returns:
        操作结果描述
    """
    try:
        with get_session() as session:
            row = session.query(EntityRelation).filter(
                EntityRelation.id == relation_id,
                EntityRelation.is_deleted == false(),
            ).first()
            if row is None:
                return f"错误: 关系 id={relation_id} 不存在"
            if row.review_status != "pending":
                return f"错误: 关系 id={relation_id} 状态为 {row.review_status}"

        _approve_relation(relation_id, reviewer=reviewer, note=note)
        logger.info(f"[AgentTool] 已批准关系: id={relation_id}")
        return f"已批准: 关系 id={relation_id}"
    except Exception as e:
        logger.error(f"[AgentTool] approve_relation 失败: {e}")
        return f"批准失败: {str(e)}"


@tool
def reject_relation(relation_id: int, reviewer: str = "auto", note: str = "") -> str:
    """拒绝关系。

    Args:
        relation_id: EntityRelation 主键
        reviewer: 审核人标识
        note: 拒绝原因

    Returns:
        操作结果描述
    """
    try:
        with get_session() as session:
            row = session.query(EntityRelation).filter(
                EntityRelation.id == relation_id,
                EntityRelation.is_deleted == false(),
            ).first()
            if row is None:
                return f"错误: 关系 id={relation_id} 不存在"
            if row.review_status != "pending":
                return f"错误: 关系 id={relation_id} 状态为 {row.review_status}"

        _reject_relation(relation_id, reviewer=reviewer, note=note)
        logger.info(f"[AgentTool] 已拒绝关系: id={relation_id}")
        return f"已拒绝: 关系 id={relation_id}"
    except Exception as e:
        logger.error(f"[AgentTool] reject_relation 失败: {e}")
        return f"拒绝失败: {str(e)}"


@tool
def resolve_attribute_conflict(
    merge_id: int,
    resolution: str,
    resolved_value: str = "",
    reviewer: str = "auto",
    reason: str = "",
    confidence: float = 0.0,
) -> str:
    """提交属性冲突的待审建议，不修改字典或冲突的实际状态。

    Args:
        merge_id: 冲突记录 ID（来自 merge-queue）
        resolution: keep_existing（保留现有值）| accept_new（接受新值）| custom（自定义值）
        resolved_value: resolution=custom 时的自定义值
        reviewer: 旧参数兼容；机器工具不取得人工审核身份
        reason: 建议理由
        confidence: 模型给出的置信度，未知为0

    Returns:
        建议JSON，由AgentRunner统一保存为待审AgentRun
    """
    if resolution not in {"keep_existing", "accept_new", "custom", "escalated"}:
        return "错误: 未知属性冲突建议"
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return "错误: confidence 必须在0至1之间"
    if resolution == "custom" and not resolved_value:
        return "错误: custom 建议必须提供自定义值"
    return json.dumps({
        "merge_id": merge_id, "decision_type": "proposal", "proposed_decision": resolution,
        "resolved_value": resolved_value, "reason": reason, "confidence": confidence,
        "applied": False,
    }, ensure_ascii=False)


@tool
def submit_final_decision(decision: str, reason: str, confidence: float) -> str:
    """提交最终判断，结束审核流程。必须在审核流程的最后一步调用。

    Args:
        decision: approved（通过）| rejected（拒绝）| escalated（转人工）
        reason: 完整的判断理由，会记录到 review_log
        confidence: 决策置信度 (0.0-1.0)，0.0 表示完全不确定，1.0 表示完全确定

    Returns:
        确认信息
    """
    valid_decisions = {"approved", "rejected", "escalated"}
    if decision not in valid_decisions:
        return f"错误: decision 必须是 {valid_decisions} 之一，收到: {decision}"
    if not 0.0 <= confidence <= 1.0:
        return f"错误: confidence 必须在 0.0-1.0 之间，收到: {confidence}"

    logger.info(f"[AgentTool] 最终决策: {decision}, confidence={confidence}, reason={reason[:100]}")
    return f"最终决策已提交: {decision}（置信度: {confidence:.2f}）"


@tool
def submit_merge_decision(decision: str, dict_id: int, reason: str, confidence: float) -> str:
    """提交实体合并决策——判断 extracted_entity 应链接到哪个 entity_dict。

    Args:
        decision: linked（已找到匹配，应链接到 dict_id）| escalated（不确定，转人工审核）
        dict_id: decision=linked 时必填，目标 entity_dict 的 ID；decision=escalated 时填 0
        reason: 完整的判断理由
        confidence: 决策置信度 (0.0-1.0)

    Returns:
        确认信息
    """
    valid_decisions = {"linked", "escalated"}
    if decision not in valid_decisions:
        return f"错误: decision 必须是 {valid_decisions} 之一，收到: {decision}"
    if not 0.0 <= confidence <= 1.0:
        return f"错误: confidence 必须在 0.0-1.0 之间，收到: {confidence}"
    if decision == "linked" and not dict_id:
        return "错误: decision=linked 时 dict_id 不能为 0"

    logger.info(f"[AgentTool] 合并决策: {decision}, dict_id={dict_id}, confidence={confidence:.2f}")
    return (
        f"decision: {decision}\n"
        f"dict_id: {dict_id}\n"
        f"confidence: {confidence:.2f}\n"
        f"reason: {reason}"
    )
