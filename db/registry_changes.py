"""临床登记差分的存取。写入和新版本在同一事务里（db.history._publish 调 record），存量用 backfill 补。"""

from __future__ import annotations

import re

from sqlalchemy import text

from biz_rules.history_models import HistoryError, canonical
from processing import registry_diff

DIFF_SOURCES = frozenset({"clinicaltrials"})


def record(conn, *, source_id, is_demo, record_id, previous, to_version_id, facts, locators, pairing,
           summary=None, raw=None):
    """记下一个新版本的差分。previous 是 {version_id, payload} 或 None（首次采用）。重复调用不重复写
    （表上有禁止改删的规则，PostgreSQL 不允许 ON CONFLICT，所以用 NOT EXISTS）。"""
    if source_id not in DIFF_SOURCES:
        return
    version = registry_diff.PROCESSOR_VERSION
    changes = []
    if previous is not None:
        from biz_rules.history_sources import stable_raw

        prior = previous["payload"] or {}
        changes = registry_diff.diff(
            prior.get("facts") or {},
            facts or {},
            locators or {},
            old_summary=prior.get("clean_text"),
            new_summary=summary,
            old_raw_stable=stable_raw(source_id, prior.get("raw_content")) if prior.get("raw_content") else None,
            new_raw_stable=stable_raw(source_id, raw) if raw else None,
        )
    inserted = conn.execute(
        text("""
          INSERT INTO derived_registry_diff_runs
            (to_version_id,processor_version,source_id,is_demo,source_record_id,from_version_id,pairing,
             change_count,material_count)
          SELECT :to,:pv,:source,:demo,:native,CAST(:from AS char(64)),:pairing,:n,:m
          WHERE NOT EXISTS (SELECT 1 FROM derived_registry_diff_runs
                            WHERE to_version_id=:to AND processor_version=:pv)
        """),
        {
            "to": to_version_id,
            "pv": version,
            "source": source_id,
            "demo": is_demo,
            "native": record_id,
            "from": previous["version_id"] if previous is not None else None,
            "pairing": pairing,
            "n": len(changes),
            "m": sum(1 for c in changes if c["material"]),
        },
    ).rowcount
    if not inserted:
        return
    for change in changes:
        conn.execute(
            text("""
              INSERT INTO derived_registry_changes
                (id,processor_version,source_id,is_demo,source_record_id,from_version_id,to_version_id,
                 field,change_kind,json_pointer,old_value,new_value,detail,material,material_reason)
              SELECT :id,:pv,:source,:demo,:native,:from,:to,:field,:kind,:pointer,
                     CAST(:old AS jsonb),CAST(:new AS jsonb),CAST(:detail AS jsonb),:material,:reason
              WHERE NOT EXISTS (SELECT 1 FROM derived_registry_changes WHERE id=:id)
            """),
            {
                "id": registry_diff.change_id(previous["version_id"], to_version_id, change["field"]),
                "pv": version,
                "source": source_id,
                "demo": is_demo,
                "native": record_id,
                "from": previous["version_id"],
                "to": to_version_id,
                "field": change["field"],
                "kind": change["change_kind"],
                "pointer": change["json_pointer"],
                "old": canonical(change["old_value"]),
                "new": canonical(change["new_value"]),
                "detail": canonical(change["detail"]),
                "material": change["material"],
                "reason": change["material_reason"],
            },
        )


def for_versions(source_id, is_demo, version_ids, mode, cutoff):
    """一页版本各自的差分。两版都须在本次模式和截止时间下可见，否则不给值（不借后来的版本泄露信息）。"""
    from db import history

    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    eligible_to = history._eligibility(mode).replace("first_seen_at", "t.first_seen_at").replace(
        "proof_available_at", "t.proof_available_at"
    )
    eligible_from = history._eligibility(mode).replace("first_seen_at", "f.first_seen_at").replace(
        "proof_available_at", "f.proof_available_at"
    )
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        history._source(conn, source_id)
        cutoff = history._cutoff(cutoff)
        if cutoff > history._now(conn):
            raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        params = {
            "source": source_id,
            "demo": is_demo,
            "ids": list(version_ids),
            "pv": registry_diff.PROCESSOR_VERSION,
            "cutoff": cutoff,
        }
        runs = conn.execute(
            text(
                "SELECT r.to_version_id,r.from_version_id,r.pairing,r.change_count,r.material_count,"
                "r.source_record_id,"
                "(r.from_version_id IS NULL OR (" + eligible_from + ")) AS from_visible "
                "FROM derived_registry_diff_runs r JOIN history_versions t ON t.version_id=r.to_version_id "
                "LEFT JOIN history_versions f ON f.version_id=r.from_version_id "
                "WHERE r.processor_version=:pv AND r.source_id=:source AND r.is_demo=:demo "
                "AND r.to_version_id=ANY(:ids) AND " + eligible_to
            ),
            params,
        ).mappings().all()
        rows = conn.execute(
            text(
                "SELECT to_version_id,field,change_kind,json_pointer,old_value,new_value,detail,material,"
                "material_reason FROM derived_registry_changes WHERE processor_version=:pv "
                "AND source_id=:source AND is_demo=:demo AND to_version_id=ANY(:ids) ORDER BY to_version_id,field"
            ),
            params,
        ).mappings().all()
    by_version: dict[str, list] = {}
    for row in rows:
        change = {k: row[k] for k in row if k != "to_version_id"}
        change["description"] = registry_diff.describe(change)
        by_version.setdefault(row["to_version_id"], []).append(change)
    out = {v: {"state": "not_processed"} for v in version_ids}
    for run in runs:
        version = run["to_version_id"]
        if not run["from_visible"]:
            out[version] = {"state": "predecessor_not_visible", "pairing": run["pairing"]}
            continue
        if run["from_version_id"] is not None:
            state = "processed"
        elif run["pairing"] == "skipped_archive_mix":
            state = "not_paired"  # 混有档案导入版本，修订先后不确定，不配对
        else:
            state = "first_adoption"
        out[version] = {
            "state": state,
            "source_record_id": run["source_record_id"],
            "from_version_id": run["from_version_id"],
            "pairing": run["pairing"],
            "change_count": run["change_count"],
            "material_count": run["material_count"],
            "changes": by_version.get(version, []),
        }
    return {
        "schema_version": "datafabric.registry_changes.v1",
        "processor_version": registry_diff.PROCESSOR_VERSION,
        "source_id": source_id,
        "mode": mode,
        "as_of": cutoff.isoformat(),
        "versions": out,
        "limitations": [
            "登记字段变化是登记方公开的修订，不等于药物成败、疗效或安全结论",
            "相邻版本按本系统采用顺序配对；两次采用之间来源可能已改过多次，中间值看不到",
            "not_processed 表示尚未差分，不表示没有变化",
        ],
    }


def backfill(source_id="clinicaltrials", *, batch=500):
    """给已采用但还没差分的观察版本补差分。按每个登记号的采用顺序配对，只用公开观察版本
    （没有档案证明的 fetch 版本）；混有档案导入版本的登记号跳过并计数，不猜修订先后。"""
    from db import history

    if source_id not in DIFF_SOURCES:
        raise HistoryError("unsupported_source", "该来源没有登记差分")
    done = skipped = 0
    while True:
        with history.history_engine().begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '60000ms'"))
            todo = conn.execute(
                text("""
                  SELECT v.version_id,v.source_record_id,v.is_demo,v.published_seq,v.payload->'facts' AS facts,
                         v.payload->'field_locators' AS locators, v.payload->>'clean_text' AS summary,
                         v.payload->>'raw_content' AS raw
                  FROM history_versions v
                  WHERE v.source_id=:source AND v.proof_available_at IS NULL
                    AND NOT EXISTS (SELECT 1 FROM derived_registry_diff_runs r
                                    WHERE r.to_version_id=v.version_id AND r.processor_version=:pv)
                  ORDER BY v.published_seq LIMIT :batch
                """),
                {"source": source_id, "pv": registry_diff.PROCESSOR_VERSION, "batch": batch},
            ).mappings().all()
            if not todo:
                return {"processed": done, "skipped_archive_mix": skipped}
            for row in todo:
                mixed = conn.execute(
                    text(
                        "SELECT 1 FROM history_versions WHERE source_id=:source AND is_demo=:demo "
                        "AND source_record_id=:native AND proof_available_at IS NOT NULL LIMIT 1"
                    ),
                    {"source": source_id, "demo": row["is_demo"], "native": row["source_record_id"]},
                ).first()
                previous = conn.execute(
                    text(
                        "SELECT version_id,payload FROM history_versions WHERE source_id=:source "
                        "AND is_demo=:demo AND source_record_id=:native AND proof_available_at IS NULL "
                        "AND published_seq<:seq ORDER BY published_seq DESC LIMIT 1"
                    ),
                    {
                        "source": source_id,
                        "demo": row["is_demo"],
                        "native": row["source_record_id"],
                        "seq": row["published_seq"],
                    },
                ).mappings().first()
                if mixed:
                    skipped += 1
                record(
                    conn,
                    source_id=source_id,
                    is_demo=row["is_demo"],
                    record_id=row["source_record_id"],
                    previous=None if mixed else previous,
                    to_version_id=row["version_id"],
                    facts=row["facts"],
                    locators=row["locators"],
                    summary=row["summary"],
                    raw=row["raw"],
                    pairing="skipped_archive_mix" if mixed else "backfill_published_seq",
                )
                done += 1
