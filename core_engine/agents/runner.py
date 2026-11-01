# -*- coding: utf-8 -*-
"""AgentRunner — 统一执行入口：Pipeline 模式（实体/关系审核）+ ReAct 模式（冲突/匹配）。"""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from core_engine.agents.config import AgentConfig
from core_engine.agents.entity_review import run_entity_review_pipeline
from core_engine.agents.relation_review import run_relation_review_pipeline
from core_engine.agents.conflict_resolver import get_conflict_resolver_graph
from core_engine.agents.semantic_matcher import get_semantic_matcher_graph
from core_engine.agents.entity_merge import get_entity_merge_graph
from db.repository import (
    create_identity_merge_proposal,
)
from db.session import get_session
from db.models import ExtractedEntity, EntityRelation, Document
from db.agent_run_repo import save_agent_run, update_agent_run_batch


class AgentRunner:
    """统一的 Agent 执行器。"""

    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig()

    # ── Agent A: 实体审核 (Pipeline 模式) ────────────────────────────────

    def run_entity_review(self, entity_id: int) -> dict:
        """运行单个实体审核。三步 Pipeline → 保存审核提案（不修改业务审核状态）。"""
        run_id = str(uuid.uuid4())[:12]
        logger.info(f"[AgentRunner] 启动实体审核: entity_id={entity_id}, run_id={run_id}")

        entity = self._load_entity(entity_id)
        if entity is None:
            return {"decision": "error", "reason": f"实体 {entity_id} 不存在", "confidence": 0.0, "run_id": run_id}

        # 运行 Pipeline
        try:
            result = run_entity_review_pipeline(entity, self.config)
        except Exception as e:
            logger.error(f"[AgentRunner] Pipeline 执行失败: entity_id={entity_id} | {e}")
            return {"decision": "error", "reason": str(e), "confidence": 0.0, "run_id": run_id}

        return self._save_review_proposal("entity", entity_id, entity, result, run_id,
                                          entity.get("entity_name", ""))

    def run_batch_entity_review(self, entity_ids: list[int]) -> list[dict]:
        """并发批量实体审核。"""
        results = []
        with ThreadPoolExecutor(max_workers=self.config.max_concurrent_reviews) as pool:
            futures = {pool.submit(self.run_entity_review, eid): eid for eid in entity_ids}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    eid = futures[future]
                    logger.error(f"[AgentRunner] 批量审核失败 entity_id={eid}: {e}")
                    results.append({"decision": "error", "reason": str(e), "confidence": 0.0})
        return results

    def run_document_review(self, document_uuid: str) -> dict:
        """按文档维度审核：先实体、后关系，并发执行，返回聚合结果。"""
        batch_id = str(uuid.uuid4())[:12]
        entities = self._get_pending_entities_by_document(document_uuid)
        relations = []  # 先声明，等实体审核完再加载

        if not entities:
            logger.info(f"[AgentRunner] 文档 {document_uuid} 无 pending 实体")
            # 即使没有实体，也可能有 pending 关系
            relations = self._get_pending_relations_by_document(document_uuid)
            if not relations:
                return {
                    "batch_id": batch_id,
                    "document_uuid": document_uuid,
                    "document_title": "",
                    "total": 0, "approved": 0, "rejected": 0, "escalated": 0, "error": 0,
                    "proposed_approved": 0, "proposed_rejected": 0, "proposed_escalated": 0,
                    "relation_total": 0, "relation_approved": 0, "relation_rejected": 0, "relation_error": 0,
                    "relation_proposed_approved": 0, "relation_proposed_rejected": 0,
                    "relation_proposed_escalated": 0, "relation_results": [],
                    "results": [],
                }

        doc_title = entities[0].get("document_title", "") if entities else ""
        logger.info(
            f"[AgentRunner] 文档维度审核: doc={doc_title} ({document_uuid}), "
            f"{len(entities)} 个实体, batch_id={batch_id}"
        )

        results = []
        with ThreadPoolExecutor(max_workers=self.config.max_concurrent_reviews) as pool:
            futures = {pool.submit(self.run_entity_review, e["id"]): e for e in entities}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    result["document_uuid"] = document_uuid
                    result["document_title"] = doc_title
                    results.append(result)
                except Exception as e:
                    ent = futures[future]
                    logger.error(f"[AgentRunner] 文档审核失败 entity_id={ent['id']}: {e}")
                    results.append({
                        "run_id": "",
                        "decision": "error",
                        "reason": str(e),
                        "confidence": 0.0,
                        "entity_name": ent.get("entity_name", ""),
                        "document_uuid": document_uuid,
                        "document_title": doc_title,
                        "steps": [],
                    })

        # This runner applies no business review decisions.
        approved = rejected = escalated = 0
        error = sum(1 for r in results if r.get("decision") == "error")

        run_ids = [r["run_id"] for r in results if r.get("run_id")]

        # ── 关系审核（实体审核完成后） ──
        relation_results = []
        if not relations:
            relations = self._get_pending_relations_by_document(document_uuid)
        if relations:
            rel_doc_title = doc_title or relations[0].get("document_title", "")
            logger.info(
                f"[AgentRunner] 文档关系审核: doc={rel_doc_title} ({document_uuid}), "
                f"{len(relations)} 个关系"
            )
            with ThreadPoolExecutor(max_workers=self.config.max_concurrent_reviews) as pool:
                rel_futures = {pool.submit(self.run_relation_review, r["id"]): r for r in relations}
                for future in as_completed(rel_futures):
                    try:
                        rel_result = future.result()
                        rel_result["document_uuid"] = document_uuid
                        rel_result["document_title"] = rel_doc_title
                        relation_results.append(rel_result)
                    except Exception as e:
                        rel = rel_futures[future]
                        logger.error(f"[AgentRunner] 文档关系审核失败 relation_id={rel['id']}: {e}")
                        relation_results.append({
                            "run_id": "",
                            "decision": "error",
                            "reason": str(e),
                            "confidence": 0.0,
                            "document_uuid": document_uuid,
                            "document_title": rel_doc_title,
                            "steps": [],
                        })
            rel_run_ids = [r["run_id"] for r in relation_results if r.get("run_id")]
            run_ids.extend(rel_run_ids)

        rel_approved = rel_rejected = 0
        rel_error = sum(1 for r in relation_results if r.get("decision") == "error")

        if run_ids:
            try:
                update_agent_run_batch(run_ids, batch_id, document_uuid)
            except Exception as e:
                logger.error(f"[AgentRunner] 批量更新 batch_id 失败: {e}")

        return {
            "batch_id": batch_id,
            "document_uuid": document_uuid,
            "document_title": doc_title or (relations[0].get("document_title", "") if relations else ""),
            "total": len(results),
            "approved": approved,
            "rejected": rejected,
            "escalated": escalated,
            "error": error,
            "proposed_approved": sum(r.get("decision") == "proposal_approved" for r in results),
            "proposed_rejected": sum(r.get("decision") == "proposal_rejected" for r in results),
            "proposed_escalated": sum(r.get("decision") == "proposal_escalated" for r in results),
            "relation_total": len(relations),
            "relation_approved": rel_approved,
            "relation_rejected": rel_rejected,
            "relation_error": rel_error,
            "relation_proposed_approved": sum(r.get("decision") == "proposal_approved" for r in relation_results),
            "relation_proposed_rejected": sum(r.get("decision") == "proposal_rejected" for r in relation_results),
            "relation_proposed_escalated": sum(r.get("decision") == "proposal_escalated" for r in relation_results),
            "results": results,
            "relation_results": relation_results,
        }

    def run_batch_pending(self, limit: int = 50, entity_type: str = "", source_site: str = "") -> list[dict]:
        """自动获取 pending 实体并批量审核。"""
        entities = self._get_pending_entities(limit, entity_type, source_site)
        if not entities:
            logger.info("[AgentRunner] 无 pending 实体可审核")
            return []
        entity_ids = [e["id"] for e in entities]
        logger.info(f"[AgentRunner] 批量审核 {len(entity_ids)} 个 pending 实体")
        return self.run_batch_entity_review(entity_ids)

    # ── Agent A 扩展: 关系审核 (Pipeline 模式) ──────────────────────────

    def run_relation_review(self, relation_id: int) -> dict:
        """运行单个关系审核。三步 Pipeline → 保存审核提案（不修改业务审核状态）。"""
        run_id = str(uuid.uuid4())[:12]
        logger.info(f"[AgentRunner] 启动关系审核: relation_id={relation_id}, run_id={run_id}")

        rel = self._load_relation(relation_id)
        if rel is None:
            return {"decision": "error", "reason": f"关系 {relation_id} 不存在", "confidence": 0.0, "run_id": run_id}

        try:
            result = run_relation_review_pipeline(rel, self.config)
        except Exception as e:
            logger.error(f"[AgentRunner] 关系 Pipeline 失败: relation_id={relation_id} | {e}")
            return {"decision": "error", "reason": str(e), "confidence": 0.0, "run_id": run_id}

        entity_name = (f"{rel.get('from_entity_name', '')} → {rel.get('to_entity_name', '')} "
                       f"({rel.get('relation_type', '')})")
        return self._save_review_proposal("relation", relation_id, rel, result, run_id, entity_name)

    def _save_review_proposal(self, kind: str, target_id: int, target: dict,
                              result: dict, run_id: str, entity_name: str) -> dict:
        """AgentRun stores the suggestion; only human review actions apply decisions."""
        proposed = result["decision"]
        allowed = ({"keep_existing", "accept_new", "custom", "escalated"} if kind == "conflict"
                   else {"approved", "rejected", "escalated"})
        decision = f"proposal_{proposed}" if proposed in allowed else "error"
        steps = list(result.get("steps") or [])
        steps.append({
            "type": "review_proposal", "target_kind": kind, "target_id": target_id,
            "proposed_decision": proposed, "applied": False,
            "review_status": target.get("review_status"),
            "model_version": getattr(self.config, "model_name", None) or None,
        })
        saved = bool(self._save_run(
            run_id=run_id, agent_type="conflict_resolution" if kind == "conflict" else f"{kind}_review", target_id=target_id,
            entity_name=entity_name, document_title=target.get("document_title", ""),
            document_uuid=target.get("document_uuid", ""), decision=decision,
            reason=result["reason"], confidence=result["confidence"], steps=steps,
        ))
        return {
            "run_id": run_id, "decision": decision if saved else "error",
            "decision_type": "proposal", "proposed_decision": proposed,
            "applied": False, "review_status": target.get("review_status"),
            "proposal_saved": saved,
            "reason": result["reason"] if saved else f"proposal persistence failed: {result['reason']}",
            "confidence": result["confidence"], "entity_name": entity_name,
            "document_title": target.get("document_title", ""), "steps": steps,
        }

    # ── Agent B: 冲突建议 (ReAct 模式) ────────────────────────────────

    def run_conflict_resolution(self, merge_id: int,
                                 dict_id: int, entity_type: str, entity_name: str,
                                 field_name: str, existing_value: Any, new_value: Any,
                                 existing_source_site: str = "", new_source_site: str = "",
                                 existing_source_doc: str = "", new_source_doc: str = "") -> dict:
        """运行属性冲突建议，统一保存AgentRun；不执行实际裁决。"""
        run_id = str(uuid.uuid4())[:12]
        logger.info(f"[AgentRunner] 冲突裁决: merge_id={merge_id}, field={field_name}")

        initial_state = {
            "merge_id": merge_id,
            "dict_id": dict_id,
            "entity_type": entity_type,
            "entity_name": entity_name,
            "field_name": field_name,
            "existing_value": str(existing_value),
            "new_value": str(new_value),
            "existing_source_site": existing_source_site,
            "new_source_site": new_source_site,
            "existing_source_doc": existing_source_doc,
            "new_source_doc": new_source_doc,
            "messages": [],
            "decision": "",
            "resolved_value": "",
            "reason": "",
            "confidence": 0.0,
        }

        graph = get_conflict_resolver_graph()
        try:
            result = graph.invoke(initial_state, config={"recursion_limit": self.config.max_iterations})
        except Exception as e:
            logger.error(f"[AgentRunner] Graph 执行失败: merge_id={merge_id} | {e}")
            return {"decision": "error", "reason": str(e), "confidence": 0.0, "run_id": run_id}

        steps = self._extract_steps(result.get("messages", []))
        value = result.get("resolved_value", "")
        steps.append({"type": "conflict_proposal", "applied": False, "resolved_value": value,
                      "input": {key: initial_state[key] for key in (
                          "merge_id", "dict_id", "entity_type", "entity_name", "field_name", "existing_value",
                          "new_value", "existing_source_site", "new_source_site", "existing_source_doc", "new_source_doc")}})
        proposal = self._save_review_proposal("conflict", merge_id, {}, {
            "decision": result.get("decision") or "escalated", "reason": result.get("reason", ""),
            "confidence": result.get("confidence", 0.0), "steps": steps,
        }, run_id, entity_name)
        proposal["resolved_value"] = value
        return proposal

    # ── Agent C: 语义匹配 (ReAct 模式, 保持不变) ────────────────────────

    def run_semantic_match(self, dict_id_a: int, dict_id_b: int,
                            entity_a_name: str = "", entity_b_name: str = "",
                            entity_type: str = "") -> dict:
        """运行语义实体匹配。"""
        run_id = str(uuid.uuid4())[:12]
        logger.info(f"[AgentRunner] 语义匹配: {dict_id_a} vs {dict_id_b}")

        initial_state = {
            "dict_id_a": dict_id_a,
            "dict_id_b": dict_id_b,
            "entity_type": entity_type,
            "entity_a_name": entity_a_name,
            "entity_b_name": entity_b_name,
            "messages": [],
            "decision": "",
            "keeper_dict_id": None,
            "reason": "",
            "confidence": 0.0,
        }

        graph = get_semantic_matcher_graph()
        try:
            result = graph.invoke(initial_state, config={"recursion_limit": self.config.max_iterations})
        except Exception as e:
            logger.error(f"[AgentRunner] Graph 执行失败: {dict_id_a} vs {dict_id_b} | {e}")
            return {"decision": "error", "reason": str(e), "confidence": 0.0, "run_id": run_id}

        steps = self._extract_steps(result.get("messages", []))
        self._save_run(run_id, "semantic_match", dict_id_a,
                       result.get("decision", "escalated"),
                       result.get("reason", ""),
                       result.get("confidence", 0.0), steps)
        return {
            "run_id": run_id,
            "decision": result.get("decision", "escalated"),
            "keeper_dict_id": result.get("keeper_dict_id"),
            "reason": result.get("reason", ""),
            "confidence": result.get("confidence", 0.0),
            "steps": steps,
        }

    # ── Agent D: 实体合并消歧 (ReAct 模式) ────────────────────────────

    def run_entity_merge(self, entity_id: int) -> dict:
        """对单个 extracted_entity 执行合并消歧——链接到已有 entity_dict 或转人工。"""
        run_id = str(uuid.uuid4())[:12]
        logger.info(f"[AgentRunner] 实体合并消歧: entity_id={entity_id}")

        entity = self._load_entity(entity_id)
        if entity is None:
            return {"decision": "error", "reason": "实体不存在", "confidence": 0.0, "run_id": run_id}

        initial_state = {
            "entity_id": entity_id,
            "entity_name": entity.get("entity_name", ""),
            "entity_type": entity.get("entity_type", ""),
            "entity_data": entity.get("entity_data", {}) or {},
            "source_text": entity.get("source_text", "") or "",
            "source_site": entity.get("source_site", "") or "",
            "document_title": entity.get("document_title", "") or "",
            "entity_dict_id": entity.get("entity_dict_id"),
            "messages": [],
            "decision": "",
            "dict_id": None,
            "reason": "",
            "confidence": 0.0,
        }

        graph = get_entity_merge_graph()
        try:
            result = graph.invoke(initial_state, config={"recursion_limit": self.config.max_iterations})
        except Exception as e:
            logger.error(f"[AgentRunner] 合并消歧失败: entity_id={entity_id} | {e}")
            return {"decision": "error", "reason": str(e), "confidence": 0.0, "run_id": run_id}

        steps = self._extract_steps(result.get("messages", []))
        decision = result.get("decision", "escalated")
        dict_id = result.get("dict_id")
        reason = result.get("reason", "")
        confidence = result.get("confidence", 0.0)

        merge_queue_id = None
        if decision == "linked" and dict_id:
            try:
                merge_queue_id = create_identity_merge_proposal(
                    source_entity_id=entity_id,
                    target_dict_id=dict_id,
                    agent_reason=reason,
                    agent_confidence=confidence,
                    agent_run_id=run_id,
                )
            except Exception as e:
                logger.error(f"[AgentRunner] 创建合并提案失败: entity_id={entity_id} | {e}")

        self._save_run(
            run_id, "entity_merge", entity_id,
            decision, reason, confidence, steps,
        )
        return {
            "run_id": run_id,
            "decision": decision,
            "dict_id": dict_id,
            "reason": reason,
            "confidence": confidence,
            "steps": steps,
            "merge_queue_id": merge_queue_id,
        }

    def run_entity_merge_stream(self, entity_id: int):
        """对单个实体执行消歧，流式产出完整 LLM 对话记录。

        Yields:
            {"type": "prompt", "system_prompt": "...", "human_prompt": "..."}
            {"type": "llm_input", "messages": [...]}     ← 每一步发给 LLM 的上下文
            {"type": "step", "step": 1, "node": "agent", "content": "...", "tool_calls": [...]}
            {"type": "step", "step": 2, "node": "tools", "tool_name": "...", "tool_input": {...}, "content": "..."}
            {"type": "step", "step": 3, "node": "remind", "content": "..."}
            {"type": "result", "decision": "linked", "dict_id": 42, "reason": "...", ...}
            {"type": "done", "run_id": "...", ...}
        """
        from langchain_core.messages import AIMessage, ToolMessage, HumanMessage, SystemMessage
        from core_engine.agents.prompts import ENTITY_MERGE_SYSTEM
        from core_engine.agents.entity_merge import _entity_merge_human_prompt

        run_id = str(uuid.uuid4())[:12]
        step_no = 0

        yield {"type": "status", "run_id": run_id, "status": "loading", "message": "加载实体…"}

        entity = self._load_entity(entity_id)
        if entity is None:
            yield {"type": "error", "run_id": run_id, "message": "实体不存在"}
            return

        initial_state = {
            "entity_id": entity_id,
            "entity_name": entity.get("entity_name", ""),
            "entity_type": entity.get("entity_type", ""),
            "entity_data": entity.get("entity_data", {}) or {},
            "source_text": entity.get("source_text", "") or "",
            "source_site": entity.get("source_site", "") or "",
            "document_title": entity.get("document_title", "") or "",
            "entity_dict_id": entity.get("entity_dict_id"),
            "messages": [],
            "decision": "",
            "dict_id": None,
            "reason": "",
            "confidence": 0.0,
        }

        entity_name = entity.get("entity_name", "")
        document_title = entity.get("document_title", "")

        # 先发出完整 prompt，让前端可以看到发给 LLM 的全部内容
        yield {
            "type": "prompt",
            "system_prompt": ENTITY_MERGE_SYSTEM,
            "human_prompt": _entity_merge_human_prompt(initial_state),
        }

        graph = get_entity_merge_graph()
        final_decision = "escalated"
        final_dict_id = None
        final_reason = ""
        final_confidence = 0.0
        all_steps = []

        # 追踪完整对话：每次 agent 节点执行前，这里就是发给 LLM 的上下文
        conversation: list = []

        def _fmt_msg(m) -> dict:
            """把 LangChain message 转成可序列化的 dict。"""
            role = type(m).__name__
            entry: dict = {"role": role}
            if isinstance(m, (SystemMessage, HumanMessage)):
                entry["content"] = m.content or ""
            elif isinstance(m, AIMessage):
                entry["content"] = m.content or ""
                tc = getattr(m, "tool_calls", None) or []
                if tc:
                    entry["tool_calls"] = [{"name": t.get("name", ""), "args": t.get("args", {})} for t in tc]
            elif isinstance(m, ToolMessage):
                entry["name"] = getattr(m, "name", "")
                entry["content"] = m.content or ""
            return entry

        try:
            yield {"type": "status", "run_id": run_id, "status": "running", "message": "Agent 开始推理…"}

            tool_call_args: dict[str, dict] = {}

            for chunk in graph.stream(initial_state, config={"recursion_limit": self.config.max_iterations}):
                for node_name, node_output in chunk.items():
                    if node_name in ("__start__",):
                        continue

                    messages = node_output.get("messages", [])
                    if not isinstance(messages, list):
                        messages = [messages]

                    if node_name == "init":
                        # init 节点：首批 SystemMessage + HumanMessage 进入对话
                        for msg in messages:
                            conversation.append(msg)

                    elif node_name == "agent":
                        # 当前 conversation 就是发给 LLM 的全部上下文
                        yield {
                            "type": "llm_input",
                            "messages": [_fmt_msg(m) for m in conversation],
                        }

                        for msg in messages:
                            if not isinstance(msg, AIMessage):
                                continue
                            content = msg.content or ""
                            tool_calls_raw = getattr(msg, "tool_calls", None) or []
                            tool_names = [tc.get("name", "") for tc in tool_calls_raw]
                            for tc in tool_calls_raw:
                                tool_call_args[tc.get("id", "")] = tc.get("args", {})
                            step_no += 1
                            step = {"step": step_no, "node": "agent", "content": content, "tool_calls": tool_names}
                            all_steps.append(step)
                            yield {"type": "step", **step}
                            conversation.append(msg)

                    elif node_name == "tools":
                        for msg in messages:
                            if not isinstance(msg, ToolMessage):
                                continue
                            tool_name = getattr(msg, "name", "")
                            tool_call_id = getattr(msg, "tool_call_id", "")
                            tool_input = tool_call_args.pop(tool_call_id, {})
                            step = {
                                "step": step_no,
                                "node": "tools",
                                "tool_name": tool_name,
                                "tool_input": tool_input,
                                "content": msg.content or "",
                            }
                            all_steps.append(step)
                            yield {"type": "step", **step}
                            conversation.append(msg)

                    elif node_name == "remind":
                        for msg in messages:
                            if isinstance(msg, HumanMessage):
                                step_no += 1
                                step = {"step": step_no, "node": "remind", "content": msg.content or ""}
                                all_steps.append(step)
                                yield {"type": "step", **step}
                                conversation.append(msg)

                    elif node_name == "finalize":
                        final_decision = node_output.get("decision", "escalated")
                        final_dict_id = node_output.get("dict_id")
                        final_reason = node_output.get("reason", "")
                        final_confidence = node_output.get("confidence", 0.0)

        except Exception as e:
            logger.error(f"[AgentRunner] 流式消歧失败: entity_id={entity_id} | {e}")
            yield {"type": "error", "run_id": run_id, "message": str(e)}
            return

        yield {"type": "status", "run_id": run_id, "status": "finalizing", "message": "正在保存结果…"}

        # 创建合并提案 + 保存运行记录
        merge_queue_id = None
        if final_decision == "linked" and final_dict_id:
            try:
                merge_queue_id = create_identity_merge_proposal(
                    source_entity_id=entity_id,
                    target_dict_id=final_dict_id,
                    agent_reason=final_reason,
                    agent_confidence=final_confidence,
                    agent_run_id=run_id,
                )
            except Exception as e:
                logger.error(f"[AgentRunner] 创建合并提案失败: entity_id={entity_id} | {e}")

        self._save_run(
            run_id, "entity_merge", entity_id,
            final_decision, final_reason, final_confidence, all_steps,
            entity_name=entity_name, document_title=document_title,
        )

        yield {
            "type": "result",
            "run_id": run_id,
            "decision": final_decision,
            "dict_id": final_dict_id,
            "reason": final_reason,
            "confidence": final_confidence,
            "merge_queue_id": merge_queue_id,
            "steps": all_steps,
        }

        yield {"type": "done", "run_id": run_id}

    # ── 内部辅助 ───────────────────────────────────────────────────────

    def _load_entity(self, entity_id: int) -> dict | None:
        with get_session() as session:
            row = (
                session.query(
                    ExtractedEntity.id,
                    ExtractedEntity.entity_name,
                    ExtractedEntity.entity_type,
                    ExtractedEntity.entity_data,
                    ExtractedEntity.source_text,
                    ExtractedEntity.source_site,
                    ExtractedEntity.confidence_score,
                    ExtractedEntity.confidence_reason,
                    ExtractedEntity.source_doc_uuid,
                    ExtractedEntity.dict_id,
                    ExtractedEntity.review_status,
                    Document.title.label("document_title"),
                )
                .outerjoin(Document, ExtractedEntity.source_doc_uuid == Document.uuid)
                .filter(
                    ExtractedEntity.id == entity_id,
                    ExtractedEntity.is_deleted == False,
                )
                .first()
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "entity_name": row.entity_name,
                "entity_type": row.entity_type,
                "entity_data": dict(row.entity_data or {}),
                "source_text": row.source_text,
                "source_site": row.source_site,
                "confidence_score": row.confidence_score,
                "confidence_reason": row.confidence_reason,
                "document_uuid": row.source_doc_uuid,
                "document_title": row.document_title or "",
                "entity_dict_id": row.dict_id,
                "review_status": row.review_status,
            }

    def _load_relation(self, relation_id: int) -> dict | None:
        with get_session() as session:
            row = session.query(EntityRelation).filter(
                EntityRelation.id == relation_id,
                EntityRelation.is_deleted == False,
            ).first()
            if row is None:
                return None
            from_ent = session.query(ExtractedEntity).filter(
                ExtractedEntity.id == row.from_entity_id).first()
            to_ent = session.query(ExtractedEntity).filter(
                ExtractedEntity.id == row.to_entity_id).first()
            return {
                "id": row.id,
                "relation_type": row.relation_type,
                "review_status": row.review_status,
                "from_entity_name": from_ent.entity_name if from_ent else "",
                "from_entity_type": from_ent.entity_type if from_ent else "",
                "to_entity_name": to_ent.entity_name if to_ent else "",
                "to_entity_type": to_ent.entity_type if to_ent else "",
                "relation_props": dict(row.relation_props or {}),
                "source_text": row.source_text,
                "source_site": row.source_site,
                "confidence_score": row.confidence_score,
                "document_uuid": row.source_doc_uuid,
            }

    def _get_pending_entities(self, limit: int, entity_type: str = "", source_site: str = "") -> list[dict]:
        with get_session() as session:
            q = (
                session.query(
                    ExtractedEntity.id,
                    ExtractedEntity.entity_name,
                    ExtractedEntity.entity_type,
                    ExtractedEntity.entity_data,
                    ExtractedEntity.source_text,
                    ExtractedEntity.source_site,
                    ExtractedEntity.confidence_score,
                    ExtractedEntity.confidence_reason,
                    ExtractedEntity.source_doc_uuid,
                    Document.title.label("document_title"),
                )
                .outerjoin(Document, ExtractedEntity.source_doc_uuid == Document.uuid)
                .filter(
                    ExtractedEntity.review_status == "pending",
                    ExtractedEntity.is_deleted == False,
                )
            )
            if entity_type:
                q = q.filter(ExtractedEntity.entity_type == entity_type)
            if source_site:
                q = q.filter(ExtractedEntity.source_site == source_site)
            rows = q.order_by(ExtractedEntity.confidence_score.desc()).limit(limit).all()
            return [{"id": r.id, "entity_name": r.entity_name, "entity_type": r.entity_type,
                     "entity_data": dict(r.entity_data or {}), "source_text": r.source_text or "",
                     "source_site": r.source_site or "", "confidence_score": r.confidence_score or 0.0,
                     "confidence_reason": r.confidence_reason or "",
                     "document_uuid": r.source_doc_uuid, "document_title": r.document_title or ""} for r in rows]

    def _get_pending_entities_by_document(self, document_uuid: str) -> list[dict]:
        with get_session() as session:
            rows = (
                session.query(
                    ExtractedEntity.id,
                    ExtractedEntity.entity_name,
                    ExtractedEntity.entity_type,
                    ExtractedEntity.entity_data,
                    ExtractedEntity.source_text,
                    ExtractedEntity.source_site,
                    ExtractedEntity.confidence_score,
                    ExtractedEntity.confidence_reason,
                    ExtractedEntity.source_doc_uuid,
                    Document.title.label("document_title"),
                )
                .outerjoin(Document, ExtractedEntity.source_doc_uuid == Document.uuid)
                .filter(
                    ExtractedEntity.source_doc_uuid == document_uuid,
                    ExtractedEntity.review_status == "pending",
                    ExtractedEntity.is_deleted == False,
                )
                .order_by(ExtractedEntity.confidence_score.desc())
                .all()
            )
            return [{"id": r.id, "entity_name": r.entity_name, "entity_type": r.entity_type,
                     "entity_data": dict(r.entity_data or {}), "source_text": r.source_text or "",
                     "source_site": r.source_site or "", "confidence_score": r.confidence_score or 0.0,
                     "confidence_reason": r.confidence_reason or "",
                     "document_uuid": r.source_doc_uuid, "document_title": r.document_title or ""} for r in rows]

    def _get_pending_relations_by_document(self, document_uuid: str) -> list[dict]:
        with get_session() as session:
            rows = (
                session.query(
                    EntityRelation.id,
                    EntityRelation.relation_type,
                    EntityRelation.from_entity_id,
                    EntityRelation.to_entity_id,
                    EntityRelation.relation_props,
                    EntityRelation.source_text,
                    EntityRelation.source_site,
                    EntityRelation.confidence_score,
                    EntityRelation.source_doc_uuid,
                    Document.title.label("document_title"),
                )
                .outerjoin(Document, EntityRelation.source_doc_uuid == Document.uuid)
                .filter(
                    EntityRelation.source_doc_uuid == document_uuid,
                    EntityRelation.review_status == "pending",
                    EntityRelation.is_deleted == False,
                )
                .order_by(EntityRelation.confidence_score.desc())
                .all()
            )
            return [{
                "id": r.id,
                "relation_type": r.relation_type,
                "from_entity_id": r.from_entity_id,
                "to_entity_id": r.to_entity_id,
                "relation_props": dict(r.relation_props or {}),
                "source_text": r.source_text or "",
                "source_site": r.source_site or "",
                "confidence_score": r.confidence_score or 0.0,
                "document_uuid": r.source_doc_uuid,
                "document_title": r.document_title or "",
            } for r in rows]

    def _extract_steps(self, messages: list) -> list[dict]:
        """从 LangChain messages 提取步骤（仅用于 ReAct 模式：Agent B/C）。"""
        steps = []
        step_no = 0
        from langchain_core.messages import AIMessage, ToolMessage
        for msg in messages:
            if isinstance(msg, AIMessage):
                content = msg.content or ""
                tool_calls = getattr(msg, "tool_calls", None) or []
                tool_names = [tc.get("name", "") for tc in tool_calls]
                step_no += 1
                steps.append({
                    "step": step_no,
                    "type": "ai",
                    "content": content,
                    "tool_calls": tool_names,
                    "tool_inputs": [{"name": call.get("name"), "args": call.get("args")}
                                    for call in tool_calls],
                })
            elif isinstance(msg, ToolMessage):
                steps.append({
                    "step": step_no,
                    "type": "tool",
                    "tool_name": getattr(msg, "name", ""),
                    "content": msg.content or "",
                })
        return steps

    def _save_run(self, run_id: str, agent_type: str, target_id: int,
                  decision: str, reason: str, confidence: float, steps: list[dict],
                  entity_name: str = "", document_title: str = "",
                  document_uuid: str = "", batch_id: str = ""):
        try:
            return save_agent_run(
                run_id=run_id, agent_type=agent_type, target_id=target_id,
                entity_name=entity_name, document_title=document_title,
                document_uuid=document_uuid, batch_id=batch_id,
                decision=decision, reason=reason, confidence=confidence, steps=steps,
            )
        except Exception as e:
            logger.error(f"[AgentRunner] 保存运行记录失败: {e}")
            return 0


_runner: AgentRunner | None = None


def get_agent_runner() -> AgentRunner:
    global _runner
    if _runner is None:
        _runner = AgentRunner()
    return _runner
