"""Read-only facts about how each version differs from the one before it (store.version_classify).

Nothing is stored: the answer follows from the two immutable versions, so it cannot go stale. "The one before" is
the next version of the record under the rule that picks a record's current version (store.history._ranked), applied
at the requested mode and cutoff. A version the reader cannot see gets no value, and no answer depends on a
version the reader could not see.
"""

from __future__ import annotations

import re

from sqlalchemy import text

from store import history
from store.history_models import HistoryError
from store.version_classify import classify

SCHEMA = "datafabric.history.version_differences.v1"


def _previous_versions(conn, source, is_demo, mode, cutoff, record_ids):
    """version_id -> the version before it, for every visible version of the given records (None for the first)."""
    where, params, _, _ = history._read_filters(
        conn, source["source_id"], is_demo, mode, cutoff, source["published_seq"], None, sorted(record_ids), (), None
    )
    rows = conn.execute(
        text(history._ranked(where, mode) + "SELECT source_record_id,version_id FROM ranked ORDER BY source_record_id,n"),
        params,
    ).mappings()
    newest_first: dict[str, list[str]] = {}
    for row in rows:
        newest_first.setdefault(row["source_record_id"], []).append(row["version_id"])
    return {
        version: previous
        for versions in newest_first.values()
        for version, previous in zip(versions, versions[1:] + [None])
    }


def _answer(source_id, version_id, record_id, previous_of, payloads):
    if version_id not in previous_of:
        return {"state": "not_visible"}
    previous = previous_of[version_id]
    if previous is None:
        return {"state": "first_adoption", "source_record_id": record_id}
    return {
        "state": "compared",
        "source_record_id": record_id,
        "from_version_id": previous,
        **classify(source_id, payloads[previous], payloads[version_id]),
    }


def for_versions(source_id, is_demo, version_ids, mode, cutoff):
    """One page of versions, each against its previous visible version at this mode and cutoff."""
    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    history._derived_mode(source_id, mode)
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        source = history._source(conn, source_id)
        cutoff = history._cutoff(cutoff)
        if cutoff > history._now(conn):
            raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        record_of = dict(conn.execute(
            text("SELECT version_id,source_record_id FROM history_versions "
                 "WHERE version_id=ANY(:ids) AND source_id=:source AND is_demo=:demo"),
            {"ids": list(version_ids), "source": source_id, "demo": is_demo},
        ).all())
        # no record to name would make the read span the whole source
        previous_of = (
            _previous_versions(conn, source, is_demo, mode, cutoff, set(record_of.values())) if record_of else {}
        )
        compared = {v for v in version_ids if previous_of.get(v)}
        payloads = {
            row["version_id"]: row["payload"]
            for row in conn.execute(
                text("SELECT version_id,payload FROM history_versions WHERE version_id=ANY(:ids)"),
                {"ids": sorted(compared | {previous_of[v] for v in compared})},
            ).mappings()
        }
    return {
        "schema_version": SCHEMA,
        "source_id": source_id,
        "mode": mode,
        "as_of": cutoff.isoformat(),
        "versions": {v: _answer(source_id, v, record_of.get(v), previous_of, payloads) for v in version_ids},
        "limitations": [
            "只说明两个相邻可见版本之间什么变了，不判断变化是否影响某个结论",
            "相邻按本模式与截止时间下可见版本的先后；两个版本之间来源可能改过多次，中间值看不到",
        ],
    }
