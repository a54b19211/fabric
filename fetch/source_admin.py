"""来源开关的管理接口（GET / PATCH /api/fabric/admin/sources）：每个来源的可读开关 read_enabled、定时抓取开关
fetch_enabled、最近一次采集任务和各固定刷新的状态；改开关时在 fabric_source_switch_log 记一行谁、何时、改成什么。

两个开关都在资料库 fabric_sources 里（V047）：读接口看 read_enabled（store/records.py · _source），定时刷新
（fetch/source_refresh.py · refresh_once）只抓两个开关都开着的来源。
"""

from __future__ import annotations

from sqlalchemy import text

from store import records
from store.record_models import FabricError

LATEST_RUN = """
SELECT s.source_id, s.read_enabled, s.fetch_enabled, s.published_seq AS max_seq,
       r.id AS run_id, r.kind, r.state, r.created_at, r.completed_at,
       r.error->>'code' AS error_code,
       (SELECT jsonb_build_object('changed_by', l.changed_by, 'changed_at', l.changed_at) FROM fabric_source_switch_log l
        WHERE l.source_id = s.source_id ORDER BY l.changed_at DESC LIMIT 1) AS last_change
FROM fabric_sources s
LEFT JOIN LATERAL (SELECT * FROM fabric_runs WHERE source_id = s.source_id ORDER BY created_at DESC LIMIT 1) r ON true
ORDER BY s.source_id
"""


def _watch_states(conn):
    """{source_id: [{watch_id, state, next_check_at, last_success_at, last_failure_at, error}]} of the configured
    watches; a broken DATAFABRIC_WATCHES gives {} (the scheduler reports it on its own)."""
    from fetch.source_refresh import _key, watches

    try:
        configured = {_key(watch): watch for watch in watches()}
    except FabricError:
        return {}
    saved = dict(conn.execute(text("SELECT watch_key, state FROM fabric_source_watches WHERE watch_key = ANY(:k)"),
                              {"k": list(configured)}).all())
    out = {}
    for key, watch in configured.items():
        state = saved.get(key, {})
        out.setdefault(watch.request.source_id, []).append(
            {"watch_id": watch.id, **{name: state.get(name) for name in (
                "state", "next_check_at", "last_success_at", "last_failure_at", "error")}})
    return out


def list_sources() -> dict:
    with records.fabric_engine().begin() as conn:
        rows = list(conn.execute(text(LATEST_RUN)).mappings())
        watches = _watch_states(conn)
    sources = []
    for row in rows:
        run = None if row["run_id"] is None else {
            "run_id": str(row["run_id"]), "kind": row["kind"], "state": row["state"],
            "created_at": row["created_at"].isoformat(),
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
            "error_code": row["error_code"]}
        sources.append({"source_id": row["source_id"], "read_enabled": row["read_enabled"],
                        "fetch_enabled": row["fetch_enabled"], "max_seq": row["max_seq"], "latest_run": run,
                        "watches": watches.get(row["source_id"], []), "last_change": row["last_change"]})
    return {"sources": sources}


def set_switches(source_id: str, changed_by: str, read_enabled: bool | None, fetch_enabled: bool | None) -> dict:
    """Change one source's switches (a None leaves that one as it is) and log the change; the row as it is now."""
    if read_enabled is None and fetch_enabled is None:
        raise FabricError("nothing_to_change", "read_enabled 和 fetch_enabled 至少给一个", 422)
    with records.fabric_engine().begin() as conn:
        row = conn.execute(text(
            "UPDATE fabric_sources SET read_enabled = COALESCE(:r, read_enabled), "
            "fetch_enabled = COALESCE(:f, fetch_enabled) WHERE source_id = :id "
            "RETURNING source_id, read_enabled, fetch_enabled"),
            {"id": source_id, "r": read_enabled, "f": fetch_enabled}).mappings().first()
        if row is None:
            raise FabricError("source_not_found", "没有这个来源", 404)
        changed_at = conn.execute(text(
            "INSERT INTO fabric_source_switch_log (source_id, changed_by, read_enabled, fetch_enabled) "
            "VALUES (:id, :by, :r, :f) RETURNING changed_at"),
            {"id": source_id, "by": changed_by, "r": row["read_enabled"], "f": row["fetch_enabled"]}).scalar_one()
    return {**dict(row), "changed_by": changed_by, "changed_at": changed_at.isoformat()}
