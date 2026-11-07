# -*- coding: utf-8 -*-
"""Agent B: 属性冲突裁决 Graph — 多来源字段冲突时决定保留哪个值。"""

import json
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from core_engine.agent_config import AgentConfig
from core_engine.agent_tools import CONFLICT_RESOLUTION_TOOLS
from core_engine.agent_tools.decision_tools import resolve_attribute_conflict
from core_engine.agents.prompts import CONFLICT_RESOLUTION_SYSTEM


class ConflictResolutionState(TypedDict):
    # 输入
    merge_id: int
    dict_id: int
    entity_type: str
    entity_name: str
    field_name: str
    existing_value: str
    new_value: str
    existing_source_site: str
    new_source_site: str
    existing_source_doc: str
    new_source_doc: str

    # ReAct 消息流
    messages: Annotated[list, add_messages]

    # 输出
    decision: str       # keep_existing | accept_new | custom | escalated
    resolved_value: str
    reason: str
    confidence: float


def build_conflict_resolver_graph(config: AgentConfig | None = None):
    if config is None:
        config = AgentConfig()

    model = ChatOpenAI(
        model=config.model_name,
        api_key=config.llm_api_key,
        base_url=config.llm_api_base,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    model_with_tools = model.bind_tools(CONFLICT_RESOLUTION_TOOLS)

    def _make_initial_messages(state: ConflictResolutionState) -> dict:
        human_msg = HumanMessage(content=f"""## 属性冲突

- **实体**: {state["entity_name"]} (类型: {state["entity_type"]}, dict_id={state["dict_id"]})
- **冲突字段**: {state["field_name"]}
- **现有值**: {json.dumps(state["existing_value"], ensure_ascii=False)}
  - 来源: {state.get("existing_source_site", "未知")}
- **新值**: {json.dumps(state["new_value"], ensure_ascii=False)}
  - 来源: {state.get("new_source_site", "未知")}

请裁决：保留现有值还是接受新值？先调 get_entity_detail({state["dict_id"]}) 了解实体全貌。""")
        return {"messages": [SystemMessage(content=CONFLICT_RESOLUTION_SYSTEM), human_msg]}

    def call_model(state: ConflictResolutionState) -> dict:
        response = model_with_tools.invoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state: ConflictResolutionState) -> str:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            for tc in last.tool_calls:
                if tc.get("name") in {"submit_final_decision", "resolve_attribute_conflict"}:
                    return "finalize"
            return "tools"
        return "finalize"

    def finalize(state: ConflictResolutionState) -> dict:
        # Both the current tool and the old completion name produce suggestions only.
        for msg in reversed(state["messages"]):
            if not isinstance(msg, AIMessage):
                continue
            for call in reversed(msg.tool_calls or []):
                if call.get("name") not in {"submit_final_decision", "resolve_attribute_conflict"}:
                    continue
                args = call.get("args") or {}
                decision = args.get("resolution") or args.get("decision")
                value = (state["existing_value"] if decision == "keep_existing" else
                         state["new_value"] if decision == "accept_new" else args.get("resolved_value", ""))
                try:
                    proposal = json.loads(resolve_attribute_conflict.invoke({
                        "merge_id": state["merge_id"], "resolution": decision, "resolved_value": value,
                        "reason": args.get("reason", ""), "confidence": args.get("confidence", 0.0),
                    }))
                    return {"decision": proposal["proposed_decision"], "resolved_value": proposal["resolved_value"],
                            "reason": proposal["reason"], "confidence": proposal["confidence"]}
                except (ValueError, TypeError, KeyError):
                    continue
        return {"decision": "escalated", "resolved_value": "", "reason": "未返回有效的结构化冲突建议，需人工复核",
                "confidence": 0.0}

    graph = StateGraph(ConflictResolutionState)
    graph.add_node("init", _make_initial_messages)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(CONFLICT_RESOLUTION_TOOLS))
    graph.add_node("finalize", finalize)

    graph.set_entry_point("init")
    graph.add_edge("init", "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "finalize": "finalize"})
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize", END)

    return graph.compile()


_conflict_resolver_graph = None


def get_conflict_resolver_graph():
    global _conflict_resolver_graph
    if _conflict_resolver_graph is None:
        _conflict_resolver_graph = build_conflict_resolver_graph()
    return _conflict_resolver_graph
