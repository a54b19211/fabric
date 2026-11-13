# -*- coding: utf-8 -*-
"""Agent DB 查询工具 — 包装现有 repository 函数为 LangChain tool。"""

from langchain_core.tools import tool
from loguru import logger

from graph.db.models import EntityDict
from graph.db.repository import (
    find_candidate_dicts_for_review,
    lookup_alias,
)
from graph.db.repository import (
    search_similar_entities as _search_similar_entities,
)
from graph.db.session import get_session
from graph.extraction.llm_client import build_embedding_text, generate_embedding


@tool
def fuzzy_search(name: str, entity_type: str, exclude_dict_id: int = 0) -> list[dict]:
    """模糊搜索——向量召回 + Reranker 排序，找到语义相似的候选实体。结果可能不准确，需要你自行调 get_entity_detail 验证关键属性。

    使用时机：精确搜索（exact_search）各种变体都无结果后，作为兜底尝试。

    Args:
        name: 实体名称（中文或英文）
        entity_type: 实体类型 (Drug / Company / Target / Disease / ClinicalTrial 等)
        exclude_dict_id: 排除的 dict_id（用于避免实体匹配到自己）

    Returns:
        候选实体列表，每项含 dict_id, name, entity_type, aliases, similarity_score, match_type, is_verified。
        未找到返回空列表。
    """
    try:
        candidates = find_candidate_dicts_for_review(
            entity_name=name,
            entity_type=entity_type,
            entity_data=None,
            top_k=10,
        )
        return [
            {
                "dict_id": c["dict_id"],
                "name": c.get("name", ""),
                "entity_type": c.get("entity_type", entity_type),
                "aliases": c.get("aliases", []),
                "similarity_score": c.get("score", c.get("similarity_score", 0.0)),
                "match_type": c.get("match_type", ""),
                "is_verified": c.get("is_verified", False),
            }
            for c in candidates
            if not (exclude_dict_id and c["dict_id"] == exclude_dict_id)
        ]
    except Exception as e:
        logger.error(f"[AgentTool] fuzzy_search 失败: {e}")
        return []


@tool
def exact_search(name: str, entity_type: str, exclude_dict_id: int = 0) -> dict | None:
    """精确搜索——查询别名表，检查该名称是否已被确认为某个 entity_dict 的别名。结果可信，无需额外验证。

    使用时机：你对某个变体名称有较高把握时，用它来查数据库是否已有记录。
    如果返回了 dict_id，说明该名称在数据库中已确认为该 dict_id 的别名（之前的消歧或审核已建立映射）。

    Args:
        name: 要查询的别名/名称
        entity_type: 实体类型
        exclude_dict_id: 排除的 dict_id（用于避免实体匹配到自己）

    Returns:
        {"dict_id": int, "canonical_name": str} 或 None（未找到）
    """
    try:
        dict_id = lookup_alias(name, entity_type)
        if dict_id is None:
            return None
        if exclude_dict_id and dict_id == exclude_dict_id:
            return None
        with get_session() as session:
            row = session.query(EntityDict).filter(EntityDict.id == dict_id).first()
            canonical_data = row.canonical_data or {} if row else {}
            canonical_name = canonical_data.get("name", "")
        return {"dict_id": dict_id, "canonical_name": canonical_name}
    except Exception as e:
        logger.error(f"[AgentTool] exact_search 失败: {e}")
        return None


@tool
def search_similar_entities(entity_name: str, entity_type: str, top_k: int = 10) -> list[dict]:
    """pgvector 向量相似度搜索——查找语义上相近的 entity_dict。

    Args:
        entity_name: 实体名称
        entity_type: 实体类型
        top_k: 返回数量上限

    Returns:
        相似实体列表，每项含 dict_id, name, similarity_score, aliases。
    """
    try:
        text = build_embedding_text(entity_name, entity_type)
        embedding = generate_embedding(text)
        if embedding is None:
            logger.warning(f"[AgentTool] embedding 生成失败: {entity_name}")
            return []
        results = _search_similar_entities(embedding, entity_type, top_k=top_k)
        return [
            {
                "dict_id": r["dict_id"],
                "name": r.get("name", ""),
                "similarity_score": r.get("similarity_score", 0.0),
                "aliases": r.get("aliases", []),
            }
            for r in results
        ]
    except Exception as e:
        logger.error(f"[AgentTool] search_similar_entities 失败: {e}")
        return []


@tool
def get_entity_detail(dict_id: int) -> dict | None:
    """获取 entity_dict 完整信息。

    Args:
        dict_id: entity_dict 主键

    Returns:
        含 canonical_name, entity_type, canonical_data, aliases, source_site, is_verified 的字典。
    """
    try:
        with get_session() as session:
            row = session.query(EntityDict).filter(EntityDict.id == dict_id).first()
            if row is None:
                return None
            aliases = []
            if hasattr(row, "aliases") and row.aliases:
                aliases = [a.alias_name for a in row.aliases if getattr(a, "alias_name", None)]
            return {
                "dict_id": row.id,
                "canonical_name": (row.canonical_data or {}).get("name", ""),
                "entity_type": row.entity_type or "",
                "canonical_data": dict(row.canonical_data or {}),
                "aliases": aliases,
                "source_site": row.canonical_source or "",
                "is_verified": bool(row.is_verified),
            }
    except Exception as e:
        logger.error(f"[AgentTool] get_entity_detail 失败: {e}")
        return None
