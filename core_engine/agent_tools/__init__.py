# -*- coding: utf-8 -*-
"""Agent 共享工具层 — LangChain @tool 装饰器定义，3 个 Agent 共用。"""

from core_engine.agent_tools.db_tools import (
    exact_search,
    fuzzy_search,
    search_similar_entities,
    get_entity_detail,
)
from core_engine.agent_tools.web_tools import web_search, web_fetch
from core_engine.agent_tools.rule_tools import get_entity_type_rules
from core_engine.agent_tools.decision_tools import (
    approve_entity,  # noqa: F401  re-exported tool
    reject_entity,  # noqa: F401  re-exported tool
    approve_relation,  # noqa: F401  re-exported tool
    reject_relation,  # noqa: F401  re-exported tool
    resolve_attribute_conflict,
    submit_final_decision,
    submit_merge_decision,
)

# Agent B — 属性冲突裁决 (ReAct 模式)
CONFLICT_RESOLUTION_TOOLS = [
    get_entity_detail,
    get_entity_type_rules,
    web_search,
    web_fetch,
    resolve_attribute_conflict,
    submit_final_decision,
]

# Agent C — 语义实体匹配
SEMANTIC_MATCH_TOOLS = [
    get_entity_detail,
    search_similar_entities,
    web_search,
    submit_final_decision,
]

# Agent D — 实体合并消歧 (ReAct 模式): 输入一个 extracted_entity，链接到 entity_dict
ENTITY_MERGE_TOOLS = [
    exact_search,
    fuzzy_search,
    get_entity_detail,
    submit_merge_decision,
]
