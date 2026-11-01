"""Machine suggestions must never execute human review actions."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core_engine.agent_runner import AgentRunner


@pytest.mark.parametrize("kind", ["entity", "relation"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_machine_review_only_saves_a_proposal(kind, decision):
    runner = AgentRunner(SimpleNamespace(max_concurrent_reviews=1, model_name="mock-model"))
    target = {"id": 9, "entity_name": "Known", "review_status": "pending"}
    pipeline_result = {"decision": decision, "reason": "model reason", "confidence": 0.99,
                       "steps": [{"type": "mock"}]}
    with ExitStack() as stack:
        stack.enter_context(patch.object(runner, f"_load_{kind}", return_value=target))
        stack.enter_context(patch(f"core_engine.agent_runner.run_{kind}_review_pipeline",
                                  return_value=pipeline_result))
        save = stack.enter_context(patch("core_engine.agent_runner.save_agent_run", return_value=123))
        mutations = [stack.enter_context(patch(f"core_engine.agent_runner.{name}", create=True))
                     for name in ("approve_entity_with_alias_writeback", "_reject_entity",
                                  "_approve_relation", "_reject_relation")]
        result = getattr(runner, f"run_{kind}_review")(9)
    for mutation in mutations:
        mutation.assert_not_called()
    assert result["decision"] == f"proposal_{decision}"
    assert result["proposed_decision"] == decision
    assert result["decision_type"] == "proposal"
    assert result["applied"] is False
    assert result["review_status"] == "pending"
    assert result["proposal_saved"] is True
    assert target["review_status"] == "pending"
    assert save.call_args.kwargs["decision"] == f"proposal_{decision}"
    assert save.call_args.kwargs["reason"] == "model reason"
    assert save.call_args.kwargs["confidence"] == 0.99


def test_document_counts_separate_suggestions_from_applied_decisions():
    runner = AgentRunner(SimpleNamespace(max_concurrent_reviews=1))
    entity_result = {"run_id": "e", "decision": "proposal_approved", "applied": False}
    relation_result = {"run_id": "r", "decision": "proposal_rejected", "applied": False}
    with patch.object(runner, "_get_pending_entities_by_document", return_value=[{"id": 1}]), \
         patch.object(runner, "_get_pending_relations_by_document", return_value=[{"id": 2}]), \
         patch.object(runner, "run_entity_review", return_value=entity_result), \
         patch.object(runner, "run_relation_review", return_value=relation_result), \
         patch("core_engine.agent_runner.update_agent_run_batch"):
        result = runner.run_document_review("doc")
    assert result["approved"] == result["rejected"] == 0
    assert result["relation_approved"] == result["relation_rejected"] == 0
    assert result["proposed_approved"] == 1
    assert result["relation_proposed_rejected"] == 1


def test_failed_save_is_reported_and_cannot_claim_a_saved_proposal():
    runner = AgentRunner(SimpleNamespace(model_name="mock-model"))
    with patch.object(runner, "_load_entity", return_value={"review_status": "pending"}), \
         patch("core_engine.agent_runner.run_entity_review_pipeline", return_value={
             "decision": "escalated", "reason": "review", "confidence": 0.5, "steps": []}), \
         patch("core_engine.agent_runner.save_agent_run", return_value=0):
        result = runner.run_entity_review(1)
    assert result["decision"] == "error"
    assert result["proposal_saved"] is False
    assert result["applied"] is False


def test_api_serialization_preserves_proposal_meaning_and_counts():
    from api.routers.agent import AgentReviewResult, DocumentReviewResponse
    proposal = AgentReviewResult(decision="proposal_approved", decision_type="proposal",
                                 proposed_decision="approved", applied=False,
                                 review_status="pending", proposal_saved=True)
    result = DocumentReviewResponse(approved=0, proposed_approved=1,
                                    relation_proposed_rejected=2, results=[proposal]).model_dump()
    assert result["proposed_approved"] == 1
    assert result["relation_proposed_rejected"] == 2
    assert result["results"][0]["proposed_decision"] == "approved"
    assert result["results"][0]["review_status"] == "pending"
    assert result["results"][0]["applied"] is False


def test_document_history_counts_proposals_separately():
    from db.agent_run_repo import list_runs_by_document

    row = SimpleNamespace(document_uuid="doc", document_title="Test", entity_count=3,
                          approved_count=0, rejected_count=0, escalated_count=0,
                          proposed_approved_count=2, proposed_rejected_count=1,
                          proposed_escalated_count=0, error_count=0,
                          avg_confidence=0.8, last_review_at=None)
    session = MagicMock()
    session.execute.side_effect = [SimpleNamespace(scalar=lambda: 1),
                                   SimpleNamespace(fetchall=lambda: [row]),
                                   SimpleNamespace(fetchall=lambda: [])]
    with patch("db.agent_run_repo.get_session") as get_session:
        get_session.return_value.__enter__.return_value = session
        documents, total = list_runs_by_document()
    assert total == 1
    assert documents[0]["approved_count"] == 0
    assert documents[0]["proposed_approved_count"] == 2
    assert documents[0]["proposed_rejected_count"] == 1


def test_document_endpoint_keeps_proposal_counts_and_does_not_change_status():
    from api.routers import agent

    proposal = {"decision": "proposal_approved", "decision_type": "proposal",
                "proposed_decision": "approved", "applied": False,
                "proposal_saved": True, "review_status": "pending"}
    session = MagicMock()
    runner = MagicMock()
    runner.run_document_review.return_value = {
        "approved": 0, "proposed_approved": 1, "relation_proposed_rejected": 2,
        "results": [proposal]}
    with patch.object(agent, "get_session") as get_session, \
         patch.object(agent, "get_agent_runner", return_value=runner):
        get_session.return_value.__enter__.return_value = session
        response = agent.trigger_document_review("doc").model_dump()
    assert response["approved"] == 0
    assert response["proposed_approved"] == 1
    assert response["relation_proposed_rejected"] == 2
    assert response["results"][0]["review_status"] == "pending"
    assert response["results"][0]["applied"] is False


def test_persisted_run_and_stats_expose_suggestions_as_suggestions():
    from api.routers import agent

    with patch.object(agent, "get_agent_run", return_value={"decision": "proposal_approved"}), \
         patch.object(agent, "get_agent_stats", return_value={"entity_review": {"proposal_approved": 1}}):
        run = agent.get_run("run")
        stats = agent.get_stats().model_dump()
    assert run["decision_type"] == "proposal"
    assert run["proposed_decision"] == "approved"
    assert run["applied"] is False
    assert stats["stats"]["entity_review"] == {"proposal_approved": 1}


def conflict_input():
    return dict(merge_id=13, dict_id=7, entity_type="Drug", entity_name="Example",
                field_name="phase", existing_value="PHASE2", new_value="PHASE3")


def test_conflict_tool_collects_a_proposal_without_resolving_or_claiming_human_identity():
    import json
    from core_engine.agent_tools import decision_tools

    with patch.object(decision_tools, "resolve_merge", create=True) as mutate:
        output = decision_tools.resolve_attribute_conflict.invoke({
            "merge_id": 13, "resolution": "accept_new", "resolved_value": "PHASE3",
            "reviewer": "pretend-human"})
    mutate.assert_not_called()
    proposal = json.loads(output)
    assert proposal["decision_type"] == "proposal"
    assert proposal["proposed_decision"] == "accept_new"
    assert proposal["applied"] is False
    assert "reviewer" not in proposal


@pytest.mark.parametrize("saved", [123, 0])
def test_conflict_runner_saves_value_and_input_as_a_pending_agent_run(saved):
    runner = AgentRunner(SimpleNamespace(max_iterations=3, model_name="mock-model"))
    result = {"decision": "accept_new", "resolved_value": "PHASE3", "reason": "source field",
              "confidence": 0.8, "messages": []}
    with patch("core_engine.agents.runner.get_conflict_resolver_graph") as graph, \
         patch("core_engine.agents.runner.save_agent_run", return_value=saved) as save:
        graph.return_value.invoke.return_value = result
        response = runner.run_conflict_resolution(**conflict_input())
    assert response["decision"] == ("proposal_accept_new" if saved else "error")
    assert response["applied"] is False
    assert response["proposal_saved"] is bool(saved)
    assert response["resolved_value"] == "PHASE3"
    assert save.call_args.kwargs["agent_type"] == "conflict_resolution"
    assert save.call_args.kwargs["decision"] == "proposal_accept_new"
    detail = next(step for step in save.call_args.kwargs["steps"] if step["type"] == "conflict_proposal")
    assert detail["resolved_value"] == "PHASE3"
    assert detail["input"]["field_name"] == "phase"
    assert detail["input"]["existing_value"] == "PHASE2"
    assert detail["input"]["new_value"] == "PHASE3"


@pytest.mark.parametrize("terminal", ["resolve_attribute_conflict", "submit_final_decision"])
def test_actual_conflict_graph_keeps_the_structured_proposed_value_without_mutation(terminal):
    from langchain_core.messages import AIMessage
    from core_engine.agents import conflict_resolver
    from core_engine.agent_tools import decision_tools

    config = SimpleNamespace(model_name="mock", llm_api_key="fake", llm_api_base="https://example.invalid",
                             temperature=0.0, max_tokens=256)
    args = {"resolution" if terminal == "resolve_attribute_conflict" else "decision": "accept_new",
            "reason": "new source value", "confidence": 0.8, "merge_id": 999}
    output = AIMessage(content="", tool_calls=[{"name": terminal, "args": args, "id": "test"}])
    with patch.object(conflict_resolver, "ChatOpenAI") as model, \
         patch.object(decision_tools, "resolve_merge", create=True) as mutate:
        model.return_value.bind_tools.return_value.invoke.return_value = output
        graph = conflict_resolver.build_conflict_resolver_graph(config)
        state = conflict_input() | {"messages": [], "existing_source_site": "", "new_source_site": "",
                                    "existing_source_doc": "", "new_source_doc": ""}
        result = graph.invoke(state)
    mutate.assert_not_called()
    assert result["decision"] == "accept_new"
    assert result["resolved_value"] == "PHASE3"
    assert result["confidence"] == 0.8
    steps = AgentRunner(SimpleNamespace())._extract_steps(result["messages"])
    assert next(step for step in steps if step["type"] == "ai")["tool_inputs"][0]["args"]["merge_id"] == 999


def test_conflict_graph_without_a_structured_proposal_escalates_instead_of_inventing_a_decision():
    from langchain_core.messages import AIMessage
    from core_engine.agents import conflict_resolver

    config = SimpleNamespace(model_name="mock", llm_api_key="fake", llm_api_base="https://example.invalid",
                             temperature=0.0, max_tokens=256)
    with patch.object(conflict_resolver, "ChatOpenAI") as model:
        model.return_value.bind_tools.return_value.invoke.return_value = AIMessage(content="accept_new?")
        result = conflict_resolver.build_conflict_resolver_graph(config).invoke(
            conflict_input() | {"messages": [], "existing_source_site": "", "new_source_site": "",
                                "existing_source_doc": "", "new_source_doc": ""})
    assert result["decision"] == "escalated"
    assert result["resolved_value"] == ""
    assert result["confidence"] == 0.0


def test_conflict_endpoint_serializes_proposal_value_and_persisted_history():
    from api.routers import agent

    result = {"decision": "proposal_accept_new", "decision_type": "proposal",
              "proposed_decision": "accept_new", "resolved_value": "PHASE3", "applied": False,
              "proposal_saved": True}
    with patch.object(agent, "get_agent_runner") as runner:
        runner.return_value.run_conflict_resolution.return_value = result
        output = agent.trigger_conflict_resolution(agent.ConflictResolutionRequest(**conflict_input())).model_dump()
    assert output["resolved_value"] == "PHASE3"
    assert output["applied"] is False
    record = agent._review_run_record({"agent_type": "conflict_resolution", "decision": "proposal_accept_new",
        "steps": [{"type": "conflict_proposal", "resolved_value": "PHASE3"}]})
    assert record["resolved_value"] == "PHASE3"
    assert record["proposed_decision"] == "accept_new"
