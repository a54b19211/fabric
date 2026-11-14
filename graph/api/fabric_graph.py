"""唐组图谱在 /api/history 下的接口：固定图版本的邻域、实体解析、路径、子图，原生图捕获，图发布状态。
雷达只用 /graph/{version_id}/resolve。"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from api.routers.history import _call, require_admin, require_read
from graph.publish import history_graph
from graph.publish.graph_tools import GraphPaths, GraphSubgraph, ReviewScope
from graph.publish.reviewed_graph import GraphNeighborhood, GraphResolution
from store import history as store
from store.history_models import Mode, RunView

router = APIRouter(prefix="/api/history", tags=["图谱"])


@router.get(
    "/graph/{version_id}/neighborhood",
    response_model=GraphNeighborhood,
    dependencies=[Depends(require_read)],
)
def graph_neighborhood(
    version_id: str,
    source_id: str,
    source_record_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    seed_ids: Annotated[list[str], Query()],
    relation_types: Annotated[list[str] | None, Query()] = None,
    direction: str = "both",
    review_scope: ReviewScope | None = None,
    max_depth: int = Query(default=1, ge=0, le=2),
    max_nodes: int = Query(default=50, ge=1, le=100),
    max_edges: int = Query(default=100, ge=1, le=200),
):
    return _call(
        history_graph.read_graph_neighborhood,
        version_id,
        source_id,
        source_record_id,
        is_demo,
        mode,
        cutoff,
        seed_ids=seed_ids,
        relation_types=relation_types or (),
        direction=direction,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        review_scope=review_scope,
    )


@router.get(
    "/graph/{version_id}/resolve",
    response_model=GraphResolution,
    dependencies=[Depends(require_read)],
)
def graph_resolve(
    version_id: str,
    source_id: str,
    source_record_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    name: str | None = None,
    external_namespace: str | None = None,
    external_id: str | None = None,
    review_scope: ReviewScope | None = None,
    limit: int = Query(default=20, ge=1, le=20),
):
    return _call(
        history_graph.read_graph_resolution,
        version_id,
        source_id,
        source_record_id,
        is_demo,
        mode,
        cutoff,
        name=name,
        external_namespace=external_namespace,
        external_id=external_id,
        review_scope=review_scope,
        limit=limit,
    )


@router.get("/publication/status", dependencies=[Depends(require_read)])
def publication_status(
    source_id: Literal["legacy_documents", "reviewed_graph"],
):
    from graph.publish.publication import publication_status as read_status

    return _call(read_status, source_id)


@router.get("/graph/{version_id}/paths", response_model=GraphPaths,
            dependencies=[Depends(require_read)])
def graph_paths(version_id: str, source_id: str, source_record_id: str, is_demo: bool,
                mode: Mode, cutoff: datetime, start: str, target: str,
                relation_types: Annotated[list[str] | None, Query()] = None,
                direction: Literal["out", "in", "both"] = "out",
                review_scope: ReviewScope = "reviewed",
                max_depth: int = Query(default=4, ge=1, le=4),
                max_paths: int = Query(default=10, ge=1, le=20),
                max_expansions: int = Query(default=1000, ge=1, le=5000)):
    return _call(history_graph.read_graph_paths, version_id, source_id, source_record_id,
                 is_demo, mode, cutoff, start=start, target=target,
                 relation_types=relation_types or (), direction=direction,
                 review_scope=review_scope, max_depth=max_depth, max_paths=max_paths,
                 max_expansions=max_expansions)


@router.get("/graph/{version_id}/subgraph", response_model=GraphSubgraph,
            dependencies=[Depends(require_read)])
def graph_subgraph(version_id: str, source_id: str, source_record_id: str, is_demo: bool,
                   mode: Mode, cutoff: datetime,
                   node_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
                   relation_types: Annotated[list[str] | None, Query()] = None,
                   review_scope: ReviewScope = "reviewed",
                   max_nodes: int = Query(default=100, ge=1, le=100),
                   max_edges: int = Query(default=200, ge=1, le=200)):
    return _call(history_graph.read_graph_subgraph, version_id, source_id, source_record_id,
                 is_demo, mode, cutoff, node_ids=node_ids,
                 relation_types=relation_types or (), review_scope=review_scope,
                 max_nodes=max_nodes, max_edges=max_edges)


class NativeGraphVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_record_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=64, max_length=64)


class NativeGraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[NativeGraphVersion] = Field(min_length=1, max_length=20)
    is_demo: bool
    mode: Mode
    cutoff: datetime
    max_nodes: int = Field(default=100, ge=1, le=100)
    max_edges: int = Field(default=200, ge=1, le=200)


@router.post("/graph/native/capture", response_model=RunView,
             dependencies=[Depends(require_admin)])
def native_graph(body: NativeGraphRequest,
                 idempotency_key: str = Header(alias="Idempotency-Key")):
    from graph.publish.graph_publish import publish_native_graph

    records = [_call(store.read_version, item.version_id, "clinicaltrials",
                     item.source_record_id, body.is_demo, body.mode, body.cutoff)
               for item in body.records]
    return _call(publish_native_graph, records, idempotency_key,
                 max_nodes=body.max_nodes, max_edges=body.max_edges)
