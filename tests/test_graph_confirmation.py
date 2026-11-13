"""审核图只包含有审计及固定证据的确认身份；旧声明仍可作为候选读。"""
from datetime import datetime, timezone

from graph.publish.graph_tools import select_bundle
from graph.publish.reviewed_graph import GraphBundle, GraphEdge, GraphNode, GraphReview


def bundle(*, audited=False, unresolved=False):
    quote = {"source_id":"external_archive","source_record_id":"D","version_id":"a"*64,
             "raw_sha256":"b"*64,"quote":"甲与乙合作","locator":{"start":0,"end":5}}
    review = {"status":"approved"}
    if audited:
        review.update(reviewed_by="fixture-reviewer", reviewed_at=datetime(2020,1,2,tzinfo=timezone.utc))
    nodes = [GraphNode(namespace="fixture", id=name, entity_type="Company", name=name,
                       identity_status="unresolved" if unresolved else "confirmed",
                       identity_review_basis="明确人工确认" if not unresolved else None,
                       review=GraphReview(**review), evidence=[quote]) for name in ["甲","乙"]]
    edge=GraphEdge(id="link",source={"namespace":"fixture","id":"甲"},target={"namespace":"fixture","id":"乙"},
                   relation_type="COOPERATES_WITH",review=GraphReview(**review),binding_status="bound",evidence=[quote])
    return GraphBundle(graph_kind="reviewed",namespace="fixture",is_demo=True,title="教学图",
                       origin="explicit_demo",scope_description="合成数据",document_refs=[],
                       nodes=nodes,edges=[edge])


def test_old_verified_flag_does_not_give_reviewed_identity():
    original=bundle()
    assert select_bundle(original,"reviewed").nodes == []
    candidate=select_bundle(original,"with_candidates")
    assert all(n.identity_status=="unresolved" for n in candidate.nodes)
    assert all(n.identity_status=="confirmed" for n in original.nodes)  # 不改写原包


def test_approved_relation_with_unknown_endpoints_is_not_reviewed_fact():
    assert select_bundle(bundle(audited=True,unresolved=True),"reviewed").edges == []


def test_audited_identity_and_bound_relation_can_be_read():
    selected=select_bundle(bundle(audited=True),"reviewed")
    assert len(selected.nodes)==2 and len(selected.edges)==1
