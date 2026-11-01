# -*- coding: utf-8 -*-
"""Agent D: 实体合并消歧 Graph — 给定一个 extracted_entity，ReAct 推理链接到 entity_dict。"""

from typing import TypedDict, Annotated
import re

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from loguru import logger

from core_engine.agent_tools import ENTITY_MERGE_TOOLS
from core_engine.agents.prompts import ENTITY_MERGE_SYSTEM
from core_engine.agents.config import AgentConfig


class EntityMergeState(TypedDict):
    entity_id: int
    entity_name: str
    entity_type: str
    entity_data: dict
    source_text: str
    source_site: str
    document_title: str
    entity_dict_id: int | None  # 该实体本身已关联的 dict_id，消歧时需排除

    messages: Annotated[list, add_messages]

    decision: str          # linked | escalated
    dict_id: int | None
    reason: str
    confidence: float


def _entity_merge_human_prompt(state: EntityMergeState) -> str:
    ed = state.get("entity_data", {}) or {}
    fields_str = "\n".join(
        f"  - {k}: {v}"
        for k, v in ed.items()
        if k not in ("_embeddings", "_disambig") and v
    )
    self_dict_id = state.get("entity_dict_id")
    self_dict_hint = ""
    if self_dict_id:
        self_dict_hint = f"""

⚠️ **重要**：本实体在审批时已创建了 entity_dict (dict_id={self_dict_id})，搜索时必须排除此 ID，不要将自己匹配给自己。调用 exact_search / fuzzy_search 时请传入 exclude_dict_id={self_dict_id}。"""

    return f"""## 实体合并消歧任务

请判断以下从文档中抽取的实体应该链接到已有的哪个 entity_dict。

### 待消歧实体

- **实体 ID**: {state["entity_id"]}
- **名称**: {state["entity_name"]}
- **类型**: {state["entity_type"]}
- **来源站**: {state.get("source_site", "")}
- **来源文档**: {state.get("document_title", "")}
- **原文片段**: {state.get("source_text", "")[:300]}

### 实体属性

{fields_str if fields_str else "（无额外属性）"}{self_dict_hint}

请开始审查。基于你的生物医药知识，思考该实体可能的别名/变体，然后使用 exact_search 和 fuzzy_search 在数据库中查找匹配。搜索完成后，必须调用 submit_merge_decision 提交最终决策。"""


def build_entity_merge_graph(config: AgentConfig | None = None):
    if config is None:
        config = AgentConfig()

    model = ChatOpenAI(
        model=config.model_name,
        api_key=config.llm_api_key,
        base_url=config.llm_api_base,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    model_with_tools = model.bind_tools(ENTITY_MERGE_TOOLS)

    def init(state: EntityMergeState) -> dict:
        return {"messages": [
            SystemMessage(content=ENTITY_MERGE_SYSTEM),
            HumanMessage(content=_entity_merge_human_prompt(state)),
        ]}

    def call_model(state: EntityMergeState) -> dict:
        response = model_with_tools.invoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state: EntityMergeState) -> str:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            for tc in last.tool_calls:
                if tc.get("name") == "submit_merge_decision":
                    return "finalize"
            return "tools"
        # LLM 输出了自由文本而非调工具 — 最多提醒一次
        remind_count = sum(
            1 for m in state["messages"]
            if isinstance(m, HumanMessage) and "不要再做文本分析了" in (getattr(m, "content", "") or "")
        )
        if remind_count == 0:
            return "remind"
        logger.error("[entity_merge] LLM 提醒后仍未调工具，强制 finalize")
        return "finalize"

    def finalize(state: EntityMergeState) -> dict:
        for msg in reversed(state["messages"]):
            if isinstance(msg, ToolMessage) and msg.name == "submit_merge_decision":
                content = msg.content or ""
                dm = re.search(r'decision[:：]\s*(\w+)', content)
                did = re.search(r'dict_id[:：=]\s*(\d+)', content)
                cm = re.search(r'confidence[:：]\s*([\d.]+)', content)
                return {
                    "decision": dm.group(1) if dm else "escalated",
                    "dict_id": int(did.group(1)) if did else None,
                    "reason": content,
                    "confidence": float(cm.group(1)) if cm else 0.5,
                }
        for msg in reversed(state["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                content = msg.content.strip()
                if any(kw in content.lower() for kw in ["linked", "链接到", "已匹配"]):
                    return {"decision": "linked", "dict_id": None, "reason": content, "confidence": 0.7}
                break
        return {"decision": "escalated", "dict_id": None, "reason": "", "confidence": 0.5}

    def remind(state: EntityMergeState) -> dict:
        return {"messages": [HumanMessage(
            content="不要再做文本分析了。根据之前的搜索结果，立即调用 submit_merge_decision 提交最终决策。"
                    "如果找到了匹配的 entity_dict，decision 填 linked，dict_id 填目标 ID；否则填 escalated，dict_id 填 0。"
                    "不要输出任何其他文字。"
        )]}

    graph = StateGraph(EntityMergeState)
    graph.add_node("init", init)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(ENTITY_MERGE_TOOLS))
    graph.add_node("finalize", finalize)
    graph.add_node("remind", remind)

    graph.set_entry_point("init")
    graph.add_edge("init", "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "finalize": "finalize", "remind": "remind"})
    graph.add_edge("tools", "agent")
    graph.add_edge("remind", "agent")
    graph.add_edge("finalize", END)

    return graph.compile()


_entity_merge_graph = None


def get_entity_merge_graph():
    global _entity_merge_graph
    if _entity_merge_graph is None:
        _entity_merge_graph = build_entity_merge_graph()
    return _entity_merge_graph
