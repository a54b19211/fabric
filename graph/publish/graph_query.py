"""受限的当前图查询：复用捕获、发布和固定版本读取，不接受Cypher。"""

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import text

from graph.publish import history_graph
from graph.publish.graph_publish import _read_primekg, capture_primekg, publish_primekg
from graph.publish.reviewed_graph import GraphNeighborhood, PrimeKGLookup, PrimeKGQuery
from store import history as store
from store.history_models import HistoryError, Model, RunView, sha256


class PrimeKGResult(Model):
    run: RunView
    query: PrimeKGQuery
    read_cutoff: str
    neighborhood: GraphNeighborhood


def _existing(key, scope):
    with store.history_engine().begin() as conn:
        store._source(conn, "primekg_graph")
        row = conn.execute(
            text("SELECT * FROM history_runs WHERE idempotency_key=:key"), {"key": key}
        ).mappings().first()
        if row is None:
            return None
        if (row["source_id"] != "primekg_graph" or row["kind"] != "archive"
                or row["scope_description"] != scope or row["is_demo"]):
            raise HistoryError("idempotency_conflict", "同一查询键已用于不同参数", 409)
        return store._view(row)


def _result(request, run):
    if run.state != "completed" or len(run.version_ids) != 1:
        raise HistoryError("graph_not_published", "查询未形成可读取的固定图版本", 409)
    with store.history_engine().begin() as conn:
        store._source(conn, "primekg_graph")
        native = conn.execute(
            text("SELECT source_record_id FROM history_versions "
                 "WHERE version_id=:id AND source_id='primekg_graph' AND is_demo=false"),
            {"id": run.version_ids[0]},
        ).scalar_one_or_none()
        cutoff = store._now(conn)
    if native is None:
        raise HistoryError("graph_not_published", "查询图版本尚不可读取", 409)
    result = history_graph.read_graph_neighborhood(
        run.version_ids[0], "primekg_graph", native, False, "current", cutoff,
        seed_ids=["primekg." + request.node_source + "|" + request.seed_id],
        max_depth=1, max_nodes=100, max_edges=200,
    )
    return PrimeKGResult(run=run, query=request, read_cutoff=cutoff.isoformat(), neighborhood=result)


def query_primekg(request: PrimeKGQuery, key: str):
    if not key or len(key) > 160 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise HistoryError("idempotency_key_required", "查询键须为1至160个可见ASCII字符", 400)
    # 重放已完成请求先核对旧回执，不能用新的observed_at重新捕获后冲突。
    stored_key = "primekg-query:" + sha256(key)
    run = _existing(stored_key, request.scope())
    if run is None:
        observation = capture_primekg(**request.model_dump())
        try:
            run = publish_primekg(observation, stored_key)
        except HistoryError as exc:
            if exc.code != "idempotency_conflict":
                raise
            # 同键并发可发生两次只读捕获；采用先提交的包，不重复发布。
            run = _existing(stored_key, request.scope())
            if run is None:
                raise
    return _result(request, run)


class PrimeKGCandidate(Model):
    name: str
    node_type: Literal["drug", "disease", "gene/protein"]
    node_source: str
    seed_id: str
    identity_status: Literal["background"] = "background"


class PrimeKGLookupResult(Model):
    query: PrimeKGLookup
    status: Literal["resolved", "ambiguous", "not_found"]
    candidates: list[PrimeKGCandidate]
    truncated: bool
    observed_at: datetime
    evidence_status: Literal["current_identity_candidates_only"] = "current_identity_candidates_only"
    limitations: list[str]


def lookup_primekg(request: PrimeKGLookup):
    labels = {"drug": "`drug`", "disease": "`disease`", "gene/protein": "`gene/protein`"}
    statement = (
        "MATCH (n:" + labels[request.node_type] + ") "
        "WHERE toLower(n.name)=toLower($name) "
        "RETURN properties(n) AS candidate LIMIT $limit_plus_one"
    )
    rows = _read_primekg(
        statement, {"name": request.name, "limit_plus_one": request.limit + 1},
        ["candidate"], request.limit + 1,
    )
    candidates = []
    try:
        for row in rows:
            raw = row[0]
            if (not isinstance(raw, dict) or raw.get("primekg_type") != request.node_type
                    or not isinstance(raw.get("name"), str)
                    or raw["name"].lower() != request.name.lower()):
                raise ValueError("图身份候选不符合本次查询")
            query = PrimeKGQuery(
                node_type=raw["primekg_type"], node_source=raw["node_source"],
                seed_id=raw["primekg_id"],
            )
            candidates.append(PrimeKGCandidate(
                name=raw["name"], node_type=query.node_type,
                node_source=query.node_source, seed_id=query.seed_id,
            ))
    except (KeyError, TypeError, ValueError):
        raise HistoryError("primekg_invalid_response", "图身份候选缺少可核对的原生字段", 503) from None
    return PrimeKGLookupResult(
        query=request,
        status="not_found" if not candidates else "resolved" if len(candidates) == 1 else "ambiguous",
        candidates=candidates[:request.limit], truncated=len(candidates) > request.limit,
        observed_at=datetime.now(timezone.utc),
        limitations=[
            "仅为当前PrimeKG中同类型、精确名称（不区分大小写）的候选；不覆盖别名或缩写",
            "resolved仅表示一个候选，不表示身份经专业审核；重名不能自动选择",
            "不发布候选包，不提供过去身份已知的证明；历史调查仍须使用当时已发布的固定图版本",
        ],
    )
