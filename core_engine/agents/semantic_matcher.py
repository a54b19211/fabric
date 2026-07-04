# -*- coding: utf-8 -*-
"""Agent C: 语义实体匹配 Graph — 判断两个 entity_dict 是否为同一实体，建议合并。"""

from typing import TypedDict, Annotated
import json

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from loguru import logger

from core_engine.agent_tools import SEMANTIC_MATCH_TOOLS
from core_engine.agents.prompts import SEMANTIC_MATCH_SYSTEM
from core_engine.agent_config import AgentConfig


class SemanticMatchState(TypedDict):
    # 输入
    dict_id_a: int
    dict_id_b: int
    entity_type: str

    # 双方数据（get_entity_detail 结果会存入 messages，这里放摘要）
    entity_a_name: str
    entity_b_name: str

    # ReAct 消息流
    messages: Annotated[list, add_messages]

    # 输出
    decision: str            # approved（应合并）| rejected（不应合并）| escalated
    keeper_dict_id: int | None
    reason: str
    confidence: float


def build_semantic_matcher_graph(config: AgentConfig | None = None):
    if config is None:
        config = AgentConfig()

    model = ChatOpenAI(
        model=config.model_name,
        api_key=config.llm_api_key,
        base_url=config.llm_api_base,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    model_with_tools = model.bind_tools(SEMANTIC_MATCH_TOOLS)

    def _make_initial_messages(state: SemanticMatchState) -> dict:
        human_msg = HumanMessage(content=f"""## 语义匹配任务

判断以下两个实体是否为同一实体（应合并）：

- **实体 A**: dict_id={state["dict_id_a"]}, 名称={state.get("entity_a_name", "")}
- **实体 B**: dict_id={state["dict_id_b"]}, 名称={state.get("entity_b_name", "")}
- **类型**: {state["entity_type"]}

请开始审查。先调用 get_entity_detail 获取两个实体的完整信息对比。""")
        return {"messages": [SystemMessage(content=SEMANTIC_MATCH_SYSTEM), human_msg]}

    def call_model(state: SemanticMatchState) -> dict:
        response = model_with_tools.invoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state: SemanticMatchState) -> str:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            for tc in last.tool_calls:
                if tc.get("name") == "submit_final_decision":
                    return "finalize"
            return "tools"
        return "finalize"

    def finalize(state: SemanticMatchState) -> dict:
        import re
        for msg in reversed(state["messages"]):
            if isinstance(msg, ToolMessage) and msg.name == "submit_final_decision":
                content = msg.content or ""
                dm = re.search(r'decision[:：]\s*(\w+)', content)
                cm = re.search(r'confidence[:：]\s*([\d.]+)', content)
                return {
                    "decision": dm.group(1) if dm else "escalated",
                    "reason": content,
                    "confidence": float(cm.group(1)) if cm else 0.5,
                }
        for msg in reversed(state["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                content = msg.content.strip()
                if any(kw in content.lower() for kw in ["approved", "应合并", "同一实体"]):
                    return {"decision": "approved", "reason": content, "confidence": 0.8}
                elif any(kw in content.lower() for kw in ["rejected", "不应合并", "不同实体"]):
                    return {"decision": "rejected", "reason": content, "confidence": 0.8}
                break
        return {"decision": "escalated", "reason": "", "confidence": 0.5}

    graph = StateGraph(SemanticMatchState)
    graph.add_node("init", _make_initial_messages)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(SEMANTIC_MATCH_TOOLS))
    graph.add_node("finalize", finalize)

    graph.set_entry_point("init")
    graph.add_edge("init", "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "finalize": "finalize"})
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize", END)

    return graph.compile()


_semantic_matcher_graph = None


def get_semantic_matcher_graph():
    global _semantic_matcher_graph
    if _semantic_matcher_graph is None:
        _semantic_matcher_graph = build_semantic_matcher_graph()
    return _semantic_matcher_graph
