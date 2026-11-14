"""派生正文（V023）的处理与读取。附件解析耗时（MinerU 每份可达分钟级），不放在发布事务里。

process() 一个一个地做：
1. 领一个附件版本（_claim）：当前处理器版本下还没有成功/不支持结果、失败未满 MAX_ATTEMPTS 次、上次失败已过
   retry_after 秒；用 SELECT ... FOR NO KEY UPDATE SKIP LOCKED 锁住这一行，锁一直持有到结果写完，两个进程不会同时
   解析同一版本；进程中断时锁随事务释放，版本留给下一轮；
2. 同 sha256 的文件已有成功结果就复用；否则解析；
3. 在同一事务里写结果，attempt 取下一个号（主键防重复）；
4. 进索引由清洗段负责：附件解析成功后，清洗段给它画像时同一事务入队（store.index_publication）。
整批受 time_budget_seconds 约束，到时不再领新的。
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy import text

from clean import attachment_parse
from fetch.catalog import ATTACHMENT_SOURCES  # noqa: E402  有附件版本的来源：站点、PubMed 全文、本地导入
from store.record_models import FabricError

MAX_ATTEMPTS = 3


def _claim(conn, sources, retry_after_seconds, seen):
    """领一个待解析的附件版本并锁住（这一轮已经做过的 seen 不再领）；没有返回 None。"""
    return conn.execute(
        text("""
          SELECT v.version_id, v.source_id, v.is_demo, v.source_record_id, v.payload->'facts' AS facts
          FROM fabric_versions v
          WHERE v.source_id = ANY(:sources) AND v.payload->'facts'->>'role' = 'attachment'
            AND NOT (v.version_id = ANY(:seen))
            AND NOT EXISTS (SELECT 1 FROM derived_clean_text d WHERE d.version_id=v.version_id AND d.processor_version=:pv
                            AND (d.status<>'failed' OR d.attempt>=:max_attempts
                                 OR d.created_at > clock_timestamp() - make_interval(secs => :retry_after)))
          ORDER BY v.published_seq
          LIMIT 1
          FOR NO KEY UPDATE OF v SKIP LOCKED
        """),
        {
            "pv": attachment_parse.PROCESSOR_VERSION,
            "sources": list(sources),
            "max_attempts": MAX_ATTEMPTS,
            "retry_after": retry_after_seconds,
            "seen": list(seen),
        },
    ).mappings().first()


def _parse(conn, facts):
    """同 sha256 的文件已有成功结果就复用，否则解析这个文件。"""
    reused = conn.execute(
        text("""
          SELECT d.text, d.parse_method, d.file_ext, d.locator_kind, d.segments FROM derived_clean_text d
          JOIN fabric_versions v ON v.version_id=d.version_id
          WHERE d.processor_version=:pv AND d.status='parsed' AND v.payload->'facts'->>'sha256'=:sha
          ORDER BY d.created_at LIMIT 1
        """),
        {"pv": attachment_parse.PROCESSOR_VERSION, "sha": facts.get("sha256") or ""},
    ).mappings().first()
    if reused:
        return {"status": "parsed", "text": reused["text"], "file_ext": reused["file_ext"],
                "parse_method": "reused_same_sha256:" + str(reused["parse_method"]), "error": None,
                "attempts": ["reused_same_sha256"], "segments": reused["segments"],
                "locator_kind": reused["locator_kind"]}
    return attachment_parse.parse_file(
        store_path=facts.get("store_path"), sha256=facts.get("sha256"), file_ext=facts.get("file_ext")
    )


def _write(conn, row, result):
    """这次尝试的结果，attempt 取下一个号。"""
    attempt = conn.execute(
        text("SELECT COALESCE(max(attempt),0)+1 FROM derived_clean_text WHERE version_id=:v AND processor_version=:pv"),
        {"v": row["version_id"], "pv": attachment_parse.PROCESSOR_VERSION},
    ).scalar_one()
    body = result["text"]
    conn.execute(
        text("""
          INSERT INTO derived_clean_text
            (version_id,processor_version,attempt,source_id,is_demo,source_record_id,status,parse_method,
             parse_attempts,error,file_ext,text,chars,text_sha256,locator_kind,segments)
          VALUES (:v,:pv,:attempt,:source,:demo,:native,:status,:method,CAST(:attempts AS jsonb),:error,
                  :ext,:text,:chars,:sha,:kind,CAST(:segments AS jsonb))
        """),
        {
            "v": row["version_id"],
            "pv": attachment_parse.PROCESSOR_VERSION,
            "attempt": attempt,
            "source": row["source_id"],
            "demo": row["is_demo"],
            "native": row["source_record_id"],
            "status": result["status"],
            "method": result["parse_method"],
            "attempts": json.dumps(result.get("attempts") or []),
            "error": result["error"],
            "ext": result["file_ext"],
            "text": body,
            "chars": len(body),
            "sha": hashlib.sha256(body.encode()).hexdigest(),
            "kind": result.get("locator_kind"),
            "segments": json.dumps(result.get("segments") or [], ensure_ascii=False),
        },
    )


def process(*, batch=20, sources=ATTACHMENT_SOURCES, retry_after_seconds=3600, time_budget_seconds=1800):
    """解析最多 batch 个待处理附件（或到时间为止）；返回本批各状态计数。解析期间那一行一直锁着（见模块说明）。"""
    from store.records import fabric_engine

    engine = fabric_engine()
    began = time.monotonic()
    counts, seen = {"parsed": 0, "failed": 0, "unsupported": 0}, []
    while len(seen) < batch and time.monotonic() - began < time_budget_seconds:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout='30000ms'"))
            row = _claim(conn, sources, retry_after_seconds, seen)
            if row is None:
                break
            seen.append(row["version_id"])
            result = _parse(conn, row["facts"] or {})
            _write(conn, row, result)
        counts[result["status"]] += 1
    return counts


def for_versions(source_id, is_demo, version_ids, mode, cutoff):
    """一页版本的派生正文（当前处理器版本下最新一次尝试），含定位和父版本关系。版本须在本次模式和截止时间下可见。"""
    from store import records

    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise FabricError("query_budget", "一次最多 100 个合法版本号", 422)
    eligible = records._eligibility(mode).replace("first_seen_at", "v.first_seen_at").replace(
        "proof_available_at", "v.proof_available_at"
    )
    with records.fabric_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        records._source(conn, source_id)
        cutoff = records._cutoff(cutoff)
        if cutoff > records._now(conn):
            raise FabricError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        params = {
            "pv": attachment_parse.PROCESSOR_VERSION,
            "source": source_id,
            "demo": is_demo,
            "ids": list(version_ids),
            "cutoff": cutoff,
        }
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (d.version_id) d.version_id,d.attempt,d.status,d.parse_method,d.parse_attempts,"
                "d.error,d.file_ext,d.text,d.chars,d.text_sha256,d.processor_version,d.locator_kind,d.segments "
                "FROM derived_clean_text d JOIN fabric_versions v ON v.version_id=d.version_id "
                "WHERE d.processor_version=:pv AND d.source_id=:source AND d.is_demo=:demo "
                "AND d.version_id=ANY(:ids) AND " + eligible + " ORDER BY d.version_id, d.attempt DESC"
            ),
            params,
        ).mappings().all()
        links = conn.execute(
            text(
                "SELECT l.attachment_version_id, l.parent_record_id, l.parent_version_id, l.link_basis "
                "FROM fabric_attachment_links l "
                "LEFT JOIN fabric_versions p ON p.version_id=l.parent_version_id "
                "WHERE l.attachment_version_id=ANY(:ids) AND (l.parent_version_id IS NULL OR ("
                + eligible.replace("v.", "p.")
                + ")) ORDER BY l.created_at"
            ),
            params,
        ).mappings().all()
    out = {v: {"state": "not_visible_or_not_processed"} for v in version_ids}
    for row in rows:
        out[row["version_id"]] = {"state": "processed", **{k: row[k] for k in row if k != "version_id"}}
    for link in links:
        entry = out.get(link["attachment_version_id"])
        if entry and entry.get("state") == "processed":
            entry.setdefault("attachment_of", []).append(
                {k: link[k] for k in ("parent_record_id", "parent_version_id", "link_basis")}
            )
    return {
        "schema_version": "datafabric.derived_text.v2",
        "processor_version": attachment_parse.PROCESSOR_VERSION,
        "source_id": source_id,
        "mode": mode,
        "as_of": cutoff.isoformat(),
        "versions": out,
        "limitations": [
            "派生正文由解析器从原件生成；表格、扫描件的识别可能有误，引用时以原件为准",
            "locator_kind=converted_page 的页码是转换后 PDF 的页码，不是原文件页码；block/sheet 没有页码；"
            "jats_section（PMC 全文 XML）按节路径和 JATS 路径定位，也没有页码；unavailable 表示解析服务没给定位",
            "failed / unsupported 表示附件内容没有进入正文，不表示附件为空",
            "parent_version_id 为空表示父页面的固定版本未知（例如按旧库链接补抓）",
        ],
    }
