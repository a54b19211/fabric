"""派生正文（V023）的处理与读取。附件解析耗时（MinerU 每份可达分钟级），所以不放在发布事务里：
process() 找出还没有成功/不支持结果的附件版本，在事务外解析，再追加一行结果。"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import text

from biz_rules.history_models import HistoryError
from processing import attachment_parse

ATTACHMENT_SOURCES = ("nmpa", "cde", "chictr")
MAX_ATTEMPTS = 3


def _pending(conn, sources, batch, retry_after_seconds):
    return conn.execute(
        text("""
          SELECT v.version_id, v.source_id, v.is_demo, v.source_record_id, v.payload->'facts' AS facts,
                 COALESCE(max(d.attempt), 0) AS attempts
          FROM history_versions v
          LEFT JOIN derived_clean_text d ON d.version_id=v.version_id AND d.processor_version=:pv
          WHERE v.source_id = ANY(:sources) AND v.payload->'facts'->>'role' = 'attachment'
          GROUP BY v.version_id, v.source_id, v.is_demo, v.source_record_id, v.payload->'facts', v.published_seq
          HAVING bool_and(d.status IS NULL OR d.status='failed')
             AND COALESCE(max(d.attempt), 0) < :max_attempts
             AND (max(d.created_at) IS NULL
                  OR max(d.created_at) <= clock_timestamp() - make_interval(secs => :retry_after))
          ORDER BY v.published_seq
          LIMIT :batch
        """),
        {
            "pv": attachment_parse.PROCESSOR_VERSION,
            "sources": list(sources),
            "max_attempts": MAX_ATTEMPTS,
            "retry_after": retry_after_seconds,
            "batch": batch,
        },
    ).mappings().all()


def process(*, batch=20, sources=ATTACHMENT_SOURCES, retry_after_seconds=3600):
    """解析一批待处理附件；返回本批各状态计数。失败的版本隔 retry_after_seconds 再试，最多 MAX_ATTEMPTS 次。"""
    from db.history import history_engine

    engine = history_engine()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='30000ms'"))
        todo = _pending(conn, sources, batch, retry_after_seconds)
    counts = {"parsed": 0, "failed": 0, "unsupported": 0}
    for row in todo:
        facts = row["facts"] or {}
        with engine.begin() as conn:
            # 同一份文件（sha256 相同，例如同一附件挂在两个栏目页面下）已有成功结果：复用，不再调解析服务
            reused = conn.execute(
                text("""
                  SELECT d.text, d.parse_method, d.file_ext, d.version_id FROM derived_clean_text d
                  JOIN history_versions v ON v.version_id=d.version_id
                  WHERE d.processor_version=:pv AND d.status='parsed' AND v.payload->'facts'->>'sha256'=:sha
                  ORDER BY d.created_at LIMIT 1
                """),
                {"pv": attachment_parse.PROCESSOR_VERSION, "sha": facts.get("sha256") or ""},
            ).mappings().first()
        if reused:
            result = {"status": "parsed", "text": reused["text"], "file_ext": reused["file_ext"],
                      "parse_method": "reused_same_sha256:" + str(reused["parse_method"]), "error": None}
        else:
            result = attachment_parse.parse_file(
                store_path=facts.get("store_path"), sha256=facts.get("sha256"), file_ext=facts.get("file_ext")
            )
        body = result["text"]
        with engine.begin() as conn:
            inserted = conn.execute(
                text("""
                  INSERT INTO derived_clean_text
                    (version_id,processor_version,attempt,source_id,is_demo,source_record_id,status,parse_method,
                     error,file_ext,text,chars,text_sha256)
                  SELECT :v,:pv,:attempt,:source,:demo,:native,:status,:method,:error,:ext,:text,:chars,:sha
                  WHERE NOT EXISTS (SELECT 1 FROM derived_clean_text
                                    WHERE version_id=:v AND processor_version=:pv AND attempt=:attempt)
                """),
                {
                    "v": row["version_id"],
                    "pv": attachment_parse.PROCESSOR_VERSION,
                    "attempt": row["attempts"] + 1,
                    "source": row["source_id"],
                    "demo": row["is_demo"],
                    "native": row["source_record_id"],
                    "status": result["status"],
                    "method": result["parse_method"],
                    "error": result["error"],
                    "ext": result["file_ext"],
                    "text": body,
                    "chars": len(body),
                    "sha": hashlib.sha256(body.encode()).hexdigest(),
                },
            ).rowcount
            if inserted and result["status"] == "parsed":
                from db.index_publication import enqueue_attachment

                enqueue_attachment(conn, row["version_id"])
        if inserted:
            counts[result["status"]] += 1
    return counts


def for_versions(source_id, is_demo, version_ids, mode, cutoff):
    """一页版本的派生正文（同一处理器版本下最新一次尝试）。版本须在本次模式和截止时间下可见。"""
    from db import history

    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    eligible = history._eligibility(mode).replace("first_seen_at", "v.first_seen_at").replace(
        "proof_available_at", "v.proof_available_at"
    )
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        history._source(conn, source_id)
        cutoff = history._cutoff(cutoff)
        if cutoff > history._now(conn):
            raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (d.version_id) d.version_id,d.attempt,d.status,d.parse_method,d.error,d.file_ext,"
                "d.text,d.chars,d.text_sha256,d.processor_version FROM derived_clean_text d "
                "JOIN history_versions v ON v.version_id=d.version_id "
                "WHERE d.processor_version=:pv AND d.source_id=:source AND d.is_demo=:demo "
                "AND d.version_id=ANY(:ids) AND " + eligible + " ORDER BY d.version_id, d.attempt DESC"
            ),
            {
                "pv": attachment_parse.PROCESSOR_VERSION,
                "source": source_id,
                "demo": is_demo,
                "ids": list(version_ids),
                "cutoff": cutoff,
            },
        ).mappings().all()
    out = {v: {"state": "not_visible_or_not_processed"} for v in version_ids}
    for row in rows:
        out[row["version_id"]] = {"state": "processed", **{k: row[k] for k in row if k != "version_id"}}
    return {
        "schema_version": "datafabric.derived_text.v1",
        "processor_version": attachment_parse.PROCESSOR_VERSION,
        "source_id": source_id,
        "mode": mode,
        "as_of": cutoff.isoformat(),
        "versions": out,
        "limitations": [
            "派生正文由解析器从原件生成；表格、扫描件的识别可能有误，引用时以原件为准",
            "failed / unsupported 表示附件内容没有进入正文，不表示附件为空",
        ],
    }
