"""正文进 WeKnora 的唯一路径（v3 3.6）：清洗产物 → 索引待办 → 桥接服务 → WeKnora。

- 索引单元是一条记录的一份清洗正文：(目标, 记录, artifact_key = 处理器版本:正文摘要)，代表版本是最先产出
  这份正文的版本。以清洗段的画像为准——画像基于哪一份正文（一般版本是 processing.clean_text，附件是
  processing.attachment_parse），就索引哪一份。
- 历史版本不因“现在不是头版”失去检索资格：更正、撤回不停用索引，检索时由 Fabric 按模式和截止时间判定
  此刻可见的版本（db.retrieval）。只有两种情况停用：来源撤权（revoked）；处理器升级后该记录已没有任何版本
  的当前产物是这份正文（superseded）。
- 入队只有一个函数 enqueue_versions：清洗段写画像时同一事务调用（新增）；worker 每轮按 published_seq 水位
  补扫已画像而还没有待办的版本（回填、新加的目标）。ON CONFLICT 去重，两条路不会重复。
- 执行是独立的 worker（python -m migration.index_publication worker），不挂在发表循环上；
  新增（first_seen 在 FRESH_DAYS 天内）优先，回填保底。
- 清单带检索元数据（来源、记录、版本、时间、证明、文档类型、证据类型、编号），桥接服务写进 WeKnora，
  命中原样带回。命中只是指针：回 Fabric 校验后才给模型读（db.retrieval）。
"""

import json
import os
import re
import threading
import time
from datetime import datetime

from pydantic import Field
from sqlalchemy import text

from biz_rules.history_models import (
    HistoryError,
    HistoryRecord,
    Model,
    canonical,
    sha256,
)
from biz_rules.observations import OBSERVATION_PARSER
from biz_rules.timestamps import parse_timestamp
from core_engine.weknora_index_client import WeKnoraIndexClient, publication_id
from db import history
from db.lease import LeaseQueue, backoff, vacant
from processing import index_pieces

FRESH_DAYS = 3
MAX_IDENTIFIERS = 50
TOMBSTONES = ("withdrawn", "deleted", "retracted")


class IndexTarget(Model):
    source_instance: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=80)
    is_demo: bool
    knowledge_base_id: str = Field(min_length=1, max_length=64)
    source_scope: str = Field(min_length=1, max_length=128)

    def key(self):
        return sha256(canonical(self.model_dump()))


def targets():
    try:
        values = json.loads(os.environ.get("DATAFABRIC_INDEX_TARGETS") or "[]")
        if not isinstance(values, list) or len(values) > 16:
            raise ValueError()
        result = [IndexTarget.model_validate(item) for item in values]
        if len({item.key() for item in result}) != len(result):
            raise ValueError()
    except (ValueError, TypeError):
        raise HistoryError("index_target_configuration", "来源到知识库的索引配置无效", 503) from None
    from biz_rules.retired_sources import RETIRED

    retired = sorted({item.source_id for item in result if item.source_id in RETIRED})
    if retired:
        # a configuration error, not something to filter at search time: it is refused when the service or the worker
        # starts (api.main, run_worker) and by every caller of targets()
        raise HistoryError("index_target_retired_source",
                           "索引目标配置了已退役来源 " + ",".join(retired) + "；退役来源不能再作索引目标", 503)
    return result


def _text_processors():
    from processing import attachment_parse, clean_text

    return [clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION]


# 一批版本的当前产物：当前画像处理器的画像 + 它所基于的那份正文（同一处理器版本下最新一次成功结果）
_CURRENT = """
SELECT DISTINCT ON (p.version_id) p.version_id,p.doc_type,p.doc_category,p.evidence_types,
       c.processor_version,c.text,c.text_sha256,c.locator_kind,c.segments
FROM derived_profiles p
JOIN LATERAL (SELECT processor_version,text,text_sha256,locator_kind,segments FROM derived_clean_text
              WHERE version_id=p.version_id AND processor_version=p.text_processor_version AND status='parsed'
              ORDER BY attempt DESC LIMIT 1) c ON true
WHERE p.version_id=ANY(:ids) AND p.processor_version=:profile AND p.text_processor_version=ANY(:texts)
ORDER BY p.version_id,p.created_at DESC
"""


def current_artifacts(conn, version_ids):
    """版本 → 当前产物（画像、正文、处理器版本、摘要）；没画像、没有成功正文的不在结果里。"""
    from processing import profile

    if not version_ids:
        return {}
    return {row["version_id"]: dict(row) for row in conn.execute(text(_CURRENT), {
        "ids": list(version_ids), "profile": profile.PROCESSOR_VERSION, "texts": _text_processors()}).mappings()}


def artifact_key(artifact):
    return artifact["processor_version"] + ":" + artifact["text_sha256"]


def artifact_ready(conn, target_id, record_id, artifact):
    """这份产物整份可检索：按当前分片计划应有的每一片（不分片即整篇一条）都有 ready 条目。按完整的预期片集合
    判定，数据库里只有其中几片、而这几片都 ready 不算。worker 的交接和检索的交接用同一个判定。"""
    expected = index_pieces.keys(artifact["text"], artifact.get("segments") or [])
    ready = {k for (k,) in conn.execute(text(
        "SELECT piece_key FROM index_publications WHERE target_id=:t AND source_record_id=:n AND artifact_key=:k "
        "AND state='ready'"
    ), {"t": target_id, "n": record_id, "k": artifact_key(artifact)})}
    return expected <= ready


def _artifact_text(conn, version_id, key):
    """待办绑定的那一份正文（处理器版本:摘要）；找不到返回 None。"""
    processor, digest = key.rsplit(":", 1)
    return conn.execute(text(
        "SELECT text FROM derived_clean_text WHERE version_id=:v AND processor_version=:pv AND status='parsed' "
        "AND text_sha256=:sha ORDER BY attempt DESC LIMIT 1"
    ), {"v": version_id, "pv": processor, "sha": digest}).scalar()


def _identifiers(conn, artifacts):
    """各版本在这份正文上抽到的编号（kind:value，去重、有上限）；编号宁缺不错，缺了以后按血缘补。"""
    from processing import identifiers

    found = {}
    if artifacts:
        for row in conn.execute(text(
            "SELECT version_id,text_processor_version,kind,value FROM derived_identifiers "
            "WHERE version_id=ANY(:ids) AND processor_version=:pv ORDER BY version_id,start_offset"
        ), {"ids": list(artifacts), "pv": identifiers.PROCESSOR_VERSION}).mappings():
            if row["text_processor_version"] == artifacts[row["version_id"]]["processor_version"]:
                values = found.setdefault(row["version_id"], [])
                value = row["kind"] + ":" + row["value"]
                if value not in values and len(values) < MAX_IDENTIFIERS:
                    values.append(value)
    return found


def _time(value):
    return value.value if value is not None else None


def _meaning(value):
    return value.meaning if value is not None else None


def metadata(record, proof_available_at, artifact, identifiers):
    """检索元数据（桥接服务写进 WeKnora，命中原样带回）。值都是字符串或字符串列表；没有的不写。
    时间：published_at / source_updated_at 是来源报告的（精度见值本身：年、年-月、日期或时刻），各带 *_meaning
    说明它是什么时间（例如 GDELT 的 source_updated_at 是 gdelt_first_observed，GDELT 的观察时间，不是来源修改时间）；
    first_seen_at 是 Fabric 首次见到，proof_available_at 是有证明的可得时间（只有审核过的档案才有）。"""
    proof = record.availability_proof
    values = {
        "source_id": record.source_id, "source_record_id": record.source_record_id,
        "version_id": record.version_id,
        "published_at": _time(record.source_published_at),
        "published_at_meaning": _meaning(record.source_published_at),
        "source_updated_at": _time(record.source_updated_at),
        "source_updated_at_meaning": _meaning(record.source_updated_at),
        "first_seen_at": record.first_seen_at.isoformat(),
        "proof_available_at": proof_available_at.isoformat() if proof_available_at else None,
        "proof_kind": proof.kind, "proof_uri": proof.uri if proof.kind == "reviewed_archive" else None,
        "doc_type": artifact["doc_type"], "doc_category": artifact["doc_category"],
        "evidence_types": list(artifact["evidence_types"] or []),
        "processor_version": artifact["processor_version"], "artifact_sha256": artifact["text_sha256"],
        "identifiers": identifiers,
    }
    return {k: v for k, v in values.items() if v not in (None, [], "")}


def _manifest(target, record, artifact, meta, piece=None):
    if target["source_id"] != record.source_id or target["is_demo"] is not record.is_demo:
        raise HistoryError("index_source_space_mismatch", "索引目标与固定原件来源空间不一致", 409)
    identity = {
        "source_instance": target["source_instance"], "source_id": record.source_id,
        "source_record_id": record.source_record_id, "version_id": record.version_id,
        "raw_sha256": record.raw_sha256,
    }
    return {
        **identity, "knowledge_base_id": target["knowledge_base_id"], "is_demo": target["is_demo"],
        "source_scope": target["source_scope"],
        "raw_locator": {k: identity[k] for k in identity if k != "raw_sha256"},
        "title": (record.title or record.source_record_id)[:255],
        # 实际上传的正文：不分片时就是整份产物；分片时是这一片（上下文前缀 + 全篇里的一段）
        "content_sha256": piece["sha256"] if piece else artifact["text_sha256"],
        **({"piece": piece} if piece else {}),
        # 正文是哪一份产物：命中能回指“版本 + 这份正文”，附件还回指父页面
        "artifact_key": artifact_key(artifact), "processor_version": artifact["processor_version"],
        "artifact_sha256": artifact["text_sha256"], "locator_kind": artifact.get("locator_kind"),
        "parent_record_id": (record.facts or {}).get("parent_record_id"),
        "metadata": meta,
    }


# 桥接服务（extensions/kdd Publication，extra=forbid）认识的字段；Fabric 清单里其余的说明字段只留在本地
# is_demo：演示目标的发布必须带上（桥接按它隔离演示与真实知识库；不带等于真实）
BRIDGE_FIELDS = ("source_instance", "source_id", "source_record_id", "version_id", "raw_sha256", "artifact_key",
                 "piece", "is_demo", "knowledge_base_id", "source_scope", "raw_locator", "content_sha256", "metadata")


def _bridge_body(row, content):
    """发给桥接的请求体。is_demo 取自索引目标（权威，不依赖清单是何时冻结的）。"""
    manifest = row["manifest"]
    body = {k: manifest[k] for k in BRIDGE_FIELDS if k in manifest}
    return {**body, "is_demo": bool(row["config"]["is_demo"]), "title": manifest["title"][:255], "content": content}


def _enqueue(conn, target, record, artifact, meta, pieces):
    """一条（目标, 记录, 产物）的索引待办：同一记录里清洗正文相同的版本只建一条，代表版本是最先产出这份正文的
    版本。检索时由 Fabric 按模式和截止时间找出此刻可见的版本，它的当前产物就是这份正文才返回（db.retrieval）。
    复用键 (目标, 记录, 产物) 由数据库唯一索引保证（V028 index_publications_record_artifact）：两个事务同时入队
    同一份正文时，后一个等前一个提交后什么也不做，代表版本一经建立就不变。
    太长的产物分片（processing.index_pieces）：一片一个物理条目，唯一键带上分片身份键（规则、序号、范围、
    片摘要）；一次放得下的不分片，身份与原来一致。
    新建后，同一记录的其他待办尽快复查（处理器升级后旧产物会被判为 superseded 并停用，记录保留）。"""
    key = artifact_key(artifact)
    lane = "new" if (datetime.now(record.first_seen_at.tzinfo) - record.first_seen_at).days < FRESH_DAYS else "backfill"
    inserted = 0
    for piece in pieces or [None]:
        manifest = _manifest(target.model_dump(), record, artifact, meta, piece)
        piece_key = index_pieces.key(piece)
        ident = [target.key(), record.version_id, key] + ([piece_key] if piece else [])
        inserted += conn.execute(text("""
            INSERT INTO index_publications
              (id,target_id,version_id,source_record_id,remote_id,manifest,artifact_key,lane,piece_index,piece,
               piece_key)
            VALUES (:id,:target,:version,:native,:remote,CAST(:manifest AS jsonb),:key,:lane,:piece_index,
                    CAST(:piece AS jsonb),:piece_key)
            ON CONFLICT DO NOTHING
        """), {"id": sha256(canonical(ident)), "target": target.key(), "version": record.version_id,
                "native": record.source_record_id, "remote": publication_id(manifest), "manifest": canonical(manifest),
                "key": key, "lane": lane, "piece_index": piece["index"] if piece else 0,
                "piece": canonical(piece) if piece else None, "piece_key": piece_key}).rowcount
    if inserted:
        conn.execute(text("""
            UPDATE index_publications SET next_check_at=clock_timestamp()
            WHERE target_id=:target AND source_record_id=:native AND artifact_key<>:key AND state<>'disabled'
        """), {"target": target.key(), "native": record.source_record_id, "key": key})
    return inserted


def enqueue_versions(conn, version_ids, *, configured=None):
    """把这些版本的当前产物放进已启用索引目标的待办（同一事务）。清洗段写画像后调用；回填扫描也用它。
    没有配置索引目标时什么也不做；没画像、正文为空的版本跳过（画像后会再来）。"""
    configured = targets() if configured is None else configured
    if not configured or not version_ids:
        return 0
    enabled = {row[0] for row in conn.execute(text("SELECT id FROM index_targets WHERE enabled"))}
    wanted = [t for t in configured if t.key() in enabled]
    if not wanted:
        return 0
    artifacts = {v: a for v, a in current_artifacts(conn, version_ids).items() if a["text"].strip()}
    if not artifacts:
        return 0
    found = _identifiers(conn, artifacts)
    count = 0
    # 图谱和观测投影不进正文索引（观测另有依据可见性检查）；按版本先后入队：同一记录正文相同的几个版本，
    # 代表版本是最早的那个
    for row in conn.execute(text(
        "SELECT payload,proof_available_at FROM history_versions WHERE version_id=ANY(:ids) "
        "AND record_kind<>'graph' AND payload->>'parser_version' IS DISTINCT FROM :observation ORDER BY published_seq"
    ), {"ids": list(artifacts), "observation": OBSERVATION_PARSER}).mappings():
        record = HistoryRecord.model_validate(row["payload"])
        if (record.facts or {}).get("publication_state") in TOMBSTONES:
            continue  # 撤回/删除的墓碑版本没有可检索的内容；检索时按模式和截止时间判 withdrawn
        artifact = artifacts[record.version_id]
        meta = metadata(record, row["proof_available_at"], artifact, found.get(record.version_id, []))
        pieces = index_pieces.plan(artifact["text"], artifact.get("segments") or [])
        for target in wanted:
            if (target.source_id, target.is_demo) == (record.source_id, record.is_demo):
                count += _enqueue(conn, target, record, artifact, meta, pieces)
    return count


def _sync_targets(conn, configured):
    for target in configured:
        conn.execute(text("""
            INSERT INTO index_targets(id,config) VALUES (:id,CAST(:config AS jsonb))
            ON CONFLICT(id) DO UPDATE SET enabled=true
        """), {"id": target.key(), "config": target.model_dump_json()})
    conn.execute(
        text("UPDATE index_targets SET enabled=false WHERE NOT(id=ANY(:ids))"),
        {"ids": [item.key() for item in configured]},
    )


def scan_committed(*, batch_size=100):
    """回填扫描：按 published_seq 水位，把已画像而还没有待办的版本放进待办；落待办和推进水位在同一事务。
    还没画像的版本跳过——它们画像时由清洗段同一事务入队。"""
    if not 1 <= batch_size <= 1000:
        raise HistoryError("index_budget", "索引扫描批次须为1至1000")
    configured = targets()
    count = 0
    with history.history_engine().begin() as conn:
        _sync_targets(conn, configured)
        for target in configured:
            row = conn.execute(
                text("SELECT * FROM index_targets WHERE id=:id FOR UPDATE SKIP LOCKED"),
                {"id": target.key()},
            ).mappings().first()
            if row is None:
                continue
            try:
                history._source(conn, target.source_id)
            except HistoryError as exc:
                if exc.status == 403:
                    continue
                raise
            versions = conn.execute(text("""
                SELECT version_id,published_seq FROM history_versions
                WHERE source_id=:source AND is_demo=:demo AND published_seq>:seq
                ORDER BY published_seq LIMIT :limit
            """), {"source": target.source_id, "demo": target.is_demo,
                    "seq": row["scanned_seq"], "limit": batch_size}).mappings().all()
            if versions:
                count += enqueue_versions(conn, [v["version_id"] for v in versions], configured=[target])
                conn.execute(text("""
                    UPDATE index_targets SET scanned_seq=:seq,scanned_at=clock_timestamp() WHERE id=:id
                """), {"seq": versions[-1]["published_seq"], "id": target.key()})
    return {"enqueued": count, "configured_targets": len(configured)}

# 认领租约：长于最慢的一次桥接调用（停用最长 180 秒，加状态核对和余量）。租约短于调用就必然在结果落库前
# 失效，结果写不进去。
LEASE_SECONDS = 300
JOBS = LeaseQueue("index_publications", ("id",))
# Claiming also moves the next check past the lease (see _claim) and stamps the row.
_CLAIM_ALSO = "next_check_at=clock_timestamp()+make_interval(secs=>:_lease_seconds),updated_at=clock_timestamp()"
_CLAIM_ROW = {"alias": "p", "columns": "p.*,t.config,t.enabled", "also_set": _CLAIM_ALSO}


def _claim(lane=None, sent=None):
    """下一个到期作业。lane=new/backfill、sent=True/False 只在那一类里找（各有部分索引）；None 不限。
    认领同时把下次检查推到租约之后：处理超时、进程中断的作业排到后面，不会每次都被最先认领、挡住其后的
    整个队列（隔离实测：120 秒租约下慢停用一再失租、反复重发，1,312 条 indexing 一直轮不到）。
    正常结束时 _finish 另定下次检查。"""
    where = ""
    if lane is not None:
        where += " AND p.lane=:lane"
    if sent is not None:
        where += " AND p.first_send_at IS NOT NULL" if sent else " AND p.first_send_at IS NULL"
    with history.history_engine().begin() as conn:
        return JOBS.claim(conn, lease_seconds=LEASE_SECONDS, **_CLAIM_ROW,
                          joins="JOIN index_targets t ON t.id=p.target_id",
                          where="p.next_check_at<=clock_timestamp()" + where, params={"lane": lane},
                          order_by="p.next_check_at,p.id")


# ---- T-30 scheduling ------------------------------------------------------------------------------------
# Job classes, mutually exclusive (decided in this order, from the row's own fields):
#   R retire: disabling, or no longer wanted (desired_state not active/unknown) and not yet disabled
#   P poll:   sent (send evidence) and not settled: confirm, bind, recover, or retry an upload proven rejected
#   F first upload (lane new) / B backfill first upload (lane backfill): never sent
#   C recheck: ready, blocked or disabled entries checked again on their schedule
# Every class has a guaranteed share of each worker's turns (ROTATION); an empty class gives its turn to the next.
_SENT = "(p.first_send_at IS NOT NULL OR p.knowledge_id IS NOT NULL OR p.receipt<>'{}'::jsonb)"
# An upload stops counting as in flight only when it is settled: ready / disabled, or (not being sent again)
# its parse ended upstream or it was proven rejected before anything was created. blocked, waiting or an
# expired lease never release it - the remote outcome may still be open.
# Evidence about the current attempt only: the receipt's latest physical attempt must be a
# later one than the attempt known when this upload was reserved (attempt_floor, 0 when none was known), and its
# outcome must be the evidence itself - "rejected" for a rejection, "uploaded" for an ended parse. in_flight or
# unknown attempts, stale receipts saved after a lost response, and receipts without last_attempt release nothing;
# only ready / disabled do.
_LAST = "COALESCE((p.receipt->'last_attempt'->>'attempt')::int,0)>COALESCE(p.attempt_floor,0)"
_OUTCOME = "COALESCE(p.receipt->'last_attempt'->>'outcome','')"
_RELEASED = ("(p.state IN ('ready','disabled') OR (p.state<>'sending' AND " + _LAST + " AND ("
             "(COALESCE(p.receipt->>'upload_status','')='rejected' AND p.knowledge_id IS NULL AND "
             + _OUTCOME + " IN ('rejected','rejected_observed')) OR "
             "(COALESCE(p.receipt->>'parse_status','') IN ('completed','failed','cancelled') AND "
             + _OUTCOME + "='uploaded'))))")
_RETIRE = "(p.state='disabling' OR (COALESCE(p.desired_state,'active') NOT IN ('active','unknown') AND p.state<>'disabled'))"
CLASS_SQL = {
    "R": _RETIRE,
    "P": f"(NOT {_RETIRE} AND p.state IN ('queued','sending','indexing','waiting') AND {_SENT})",
    "F": f"(NOT {_RETIRE} AND p.lane IS DISTINCT FROM 'backfill' AND p.state IN ('queued','waiting') AND NOT {_SENT})",
    "B": f"(NOT {_RETIRE} AND p.lane='backfill' AND p.state IN ('queued','waiting') AND NOT {_SENT})",
    "C": f"(NOT {_RETIRE} AND p.state IN ('ready','blocked','disabled'))",
}
CLASS_OF = ("CASE WHEN " + CLASS_SQL["R"] + " THEN 'R' WHEN " + CLASS_SQL["P"] + " THEN 'P' WHEN " + CLASS_SQL["F"]
            + " THEN 'F' WHEN " + CLASS_SQL["B"] + " THEN 'B' WHEN " + CLASS_SQL["C"] + " THEN 'C' END")
# One worker thread's turns: first uploads 2, polls 3, retire 1, backfill 1, recheck 1 of every 8.
ROTATION = ("F", "P", "R", "F", "P", "B", "C", "P")
UPLOAD_CLASSES = ("F", "B")


def _env_number(name, default, cast=int):
    try:
        value = cast(os.environ.get(name) or default)
    except ValueError:
        raise HistoryError("index_target_configuration", name + " 必须是正数", 503) from None
    if value <= 0:
        raise HistoryError("index_target_configuration", name + " 必须是正数", 503)
    return value


def limits():
    """The shared upload budget of one knowledge base: entries / characters / bytes in flight, and the send
    bucket (uploads per minute, burst)."""
    return {"max_inflight": _env_number("DATAFABRIC_INDEX_MAX_INFLIGHT", 200),
            "max_inflight_chars": _env_number("DATAFABRIC_INDEX_MAX_INFLIGHT_CHARS", 20_000_000),
            "max_inflight_bytes": _env_number("DATAFABRIC_INDEX_MAX_INFLIGHT_BYTES", 60_000_000),
            "uploads_per_minute": _env_number("DATAFABRIC_INDEX_UPLOADS_PER_MINUTE", 60, float),
            "burst": _env_number("DATAFABRIC_INDEX_UPLOAD_BURST", 10, float)}


# An upload whose size is unknown (sent before sizes were recorded and not recoverable from its content) is never
# counted as zero: it blocks new uploads of its knowledge base until its size is known or it settles. The current
# single-upload limit used in the sums is no proof for an old upload, only a placeholder for display.
_SIZE = ("COALESCE(sum(COALESCE(p.sent_chars,:uc)),0),COALESCE(sum(COALESCE(p.sent_bytes,:ub)),0),"
         "count(*) FILTER (WHERE p.sent_chars IS NULL OR p.sent_bytes IS NULL)")


def _unknown_size():
    return {"uc": index_pieces.MAX_CHARS, "ub": index_pieces.MAX_BYTES}


def _inflight(conn, kb, exclude=None):
    """(entries, chars, bytes, entries of unknown size) in flight for a knowledge base."""
    return conn.execute(text(
        "SELECT count(*)," + _SIZE + " FROM index_publications p "
        "WHERE p.manifest->>'knowledge_base_id'=:kb AND p.state NOT IN ('ready','disabled') AND " + _SENT
        + " AND NOT " + _RELEASED + (" AND p.id<>:id" if exclude else "")
    ), {"kb": kb, "id": exclude, **_unknown_size()}).one()


def _uploaded(conn, kb):
    """What the physical uploads of a knowledge base carried so far, counted and never limited by the count. An
    entry is sent again when the bridge proved the first upload rejected before anything was created; the bridge
    numbers every attempt and attempt_floor is the number of earlier ones known when this upload was reserved, so an
    entry was sent attempt_floor + 1 times, each time with the same body (sent_chars). An entry sent without a
    recorded size is counted apart, not guessed. WeKnora batches and retries its embedding requests itself and
    reports neither."""
    row = conn.execute(text(
        "SELECT count(*) FILTER (WHERE sent_chars IS NOT NULL) AS entries,"
        "COALESCE(sum(COALESCE(attempt_floor,0)) FILTER (WHERE sent_chars IS NOT NULL),0) AS retried_uploads,"
        "COALESCE(sum(sent_chars),0) AS first_chars,COALESCE(sum(sent_chars*COALESCE(attempt_floor,0)),0) AS retried_chars,"
        "count(*) FILTER (WHERE sent_chars IS NULL) AS unsized_entries "
        "FROM index_publications WHERE manifest->>'knowledge_base_id'=:kb AND first_send_at IS NOT NULL"
    ), {"kb": kb}).mappings().one()
    return {"uploads": int(row["entries"] + row["retried_uploads"]), "chars": int(row["first_chars"] + row["retried_chars"]),
            "retried_uploads": int(row["retried_uploads"]), "retried_chars": int(row["retried_chars"]),
            "unsized_entries": row["unsized_entries"], "embedding_requests": "unknown"}


def _gated(conn, lim):
    """{knowledge base: reason} where no new upload may start now: in-flight entries or size full, or no token."""
    out = {}
    for kb, n, c, b, unknown in conn.execute(text(
        "SELECT p.manifest->>'knowledge_base_id'," "count(*)," + _SIZE + " FROM index_publications p "
        "WHERE p.state NOT IN ('ready','disabled') AND " + _SENT + " AND NOT " + _RELEASED + " GROUP BY 1"
    ), _unknown_size()):
        if n >= lim["max_inflight"]:
            out[kb] = "inflight_entries"
        elif unknown:
            out[kb] = "inflight_size_unknown"
        elif c >= lim["max_inflight_chars"] or b >= lim["max_inflight_bytes"]:
            out[kb] = "inflight_size"
    for (kb,) in conn.execute(text(
        "SELECT knowledge_base_id FROM index_send_bucket "
        "WHERE LEAST(:burst,tokens+EXTRACT(EPOCH FROM clock_timestamp()-refilled_at)*:rate/60.0)<1"
    ), {"burst": lim["burst"], "rate": lim["uploads_per_minute"]}):
        out.setdefault(kb, "send_rate")
    return out


def _gate_closed(conn, kb, reason):
    """A gate seen closed while uploads wait: count the denial (per observation) and open a wall-clock interval
    (closed_since) for the reason and for "any" - the interval, not the number of threads that saw it, is the
    blocked time, so concurrent workers do not add it up twice."""
    for r in (reason, "any"):
        conn.execute(text("""
            INSERT INTO index_gate_stats(knowledge_base_id,reason,denials,blocked_seconds,last_at,closed_since)
            VALUES (:kb,:r,1,0,clock_timestamp(),clock_timestamp())
            ON CONFLICT (knowledge_base_id,reason) DO UPDATE SET denials=index_gate_stats.denials+1,
              last_at=clock_timestamp(),closed_since=COALESCE(index_gate_stats.closed_since,clock_timestamp())
        """), {"kb": kb, "r": r})


def _gate_open(conn, kb):
    """The gate seen open: close every open interval of this knowledge base into blocked_seconds."""
    conn.execute(text("""
        UPDATE index_gate_stats SET blocked_seconds=blocked_seconds+EXTRACT(EPOCH FROM clock_timestamp()-closed_since),
          closed_since=NULL WHERE knowledge_base_id=:kb AND closed_since IS NOT NULL
    """), {"kb": kb})


def _reserve(row, content, receipt=None):
    """Before one physical upload (a first upload or the retry of one proven rejected): under the knowledge
    base's advisory lock - shared by every worker and process - check the in-flight entries, characters and
    bytes, take a send token, and mark the job sending with its size and the bridge's latest known attempt
    (attempt_floor: only evidence about a later attempt can settle this one), all in one transaction. Returns
    None when granted, or the reason it was not (nothing is sent then)."""
    floor = int(((receipt or {}).get("last_attempt") or {}).get("attempt") or 0)
    kb, lim = row["manifest"]["knowledge_base_id"], limits()
    chars, size = len(content), len(content.encode())
    with history.history_engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "index_kb:" + kb})
        n, c, b, unknown = _inflight(conn, kb, exclude=row["id"])
        reason = None
        if n + 1 > lim["max_inflight"]:
            reason = "inflight_entries"
        elif unknown:
            reason = "inflight_size_unknown"  # an earlier upload of unknown size: no new one until it is known
        elif n and (c + chars > lim["max_inflight_chars"] or b + size > lim["max_inflight_bytes"]):
            reason = "inflight_size"  # one upload alone may exceed the size budget, never two together
        if reason is None:
            bucket = conn.execute(text(
                "SELECT LEAST(:burst,tokens+EXTRACT(EPOCH FROM clock_timestamp()-refilled_at)*:rate/60.0) "
                "FROM index_send_bucket WHERE knowledge_base_id=:kb FOR UPDATE"
            ), {"kb": kb, "burst": lim["burst"], "rate": lim["uploads_per_minute"]}).scalar()
            tokens = lim["burst"] if bucket is None else float(bucket)
            if tokens < 1:
                reason = "send_rate"
            conn.execute(text("""
                INSERT INTO index_send_bucket(knowledge_base_id,tokens,refilled_at) VALUES (:kb,:t,clock_timestamp())
                ON CONFLICT (knowledge_base_id) DO UPDATE SET tokens=:t,refilled_at=clock_timestamp()
            """), {"kb": kb, "t": tokens if reason else tokens - 1})
        if reason:
            _gate_closed(conn, kb, reason)
            return reason
        _gate_open(conn, kb)
        if JOBS.update(conn, row, "first_send_at=COALESCE(first_send_at,clock_timestamp()),state='sending',"
                       "sent_chars=:chars,sent_bytes=:size,attempt_floor=:floor,updated_at=clock_timestamp()",
                       {"chars": chars, "size": size, "floor": floor}) != 1:
            raise HistoryError("index_lease_lost", "索引处理资格已改变", 409)
    return None


def _claim_class(klass):
    """The next due job of one class: the source served least recently in this class first, then by due time.
    An upload class skips knowledge bases whose gate is closed, so their jobs do not burn turns."""
    with history.history_engine().begin() as conn:
        gated = {}
        if klass in UPLOAD_CLASSES:
            # the gate before the claim: knowledge bases with due uploads are seen closed or open, and recorded
            gated = _gated(conn, limits())
            waiting = [kb for (kb,) in conn.execute(text(
                "SELECT DISTINCT p.manifest->>'knowledge_base_id' FROM index_publications p "
                "WHERE p.next_check_at<=clock_timestamp() AND " + vacant("p") + " AND " + CLASS_SQL[klass]))]
            for kb in waiting:
                if kb in gated:
                    _gate_closed(conn, kb, gated[kb])
                else:
                    _gate_open(conn, kb)
        row = JOBS.claim(
            conn, lease_seconds=LEASE_SECONDS, **_CLAIM_ROW, params={"k": klass, "gated": list(gated)},
            joins="JOIN index_targets t ON t.id=p.target_id "
                  "LEFT JOIN index_claim_cursor c ON c.class=:k AND c.source_id=t.config->>'source_id'",
            where="p.next_check_at<=clock_timestamp() AND " + CLASS_SQL[klass]
                  + " AND NOT (p.manifest->>'knowledge_base_id' = ANY(:gated))",
            order_by="c.last_claimed_at NULLS FIRST,p.next_check_at,p.id")
        if row is None:
            return None
        conn.execute(text("""
            INSERT INTO index_claim_cursor(class,source_id,last_claimed_at) VALUES (:k,:s,clock_timestamp())
            ON CONFLICT (class,source_id) DO UPDATE SET last_claimed_at=clock_timestamp()
        """), {"k": klass, "s": row["config"]["source_id"]})
        return dict(row, job_class=klass)


def rotation_gaps():
    """Per class, the longest run of claims between two of its turns in ROTATION, from any starting phase (F at
    turns 0 and 3: 5). With a fixed set of S_k sources holding due jobs of class k, its gate open and every
    claim bounded (lease LEASE_SECONDS), a due job with a jobs ahead of it in its source is claimed within
    (a + 1) * S_k * gap_k claims of one worker thread; new jobs arriving in other classes do not change it."""
    out = {}
    for k in dict.fromkeys(ROTATION):
        at = [i for i, c in enumerate(ROTATION) if c == k]
        out[k] = max((at[(j + 1) % len(at)] - at[j]) % len(ROTATION) or len(ROTATION) for j in range(len(at)))
    return out


def scheduler_status():
    """Observable progress per class x source (due, leased, oldest due wait, last claim) and per knowledge base
    (in flight against its budget, send tokens, gate denials and blocked time, characters uploaded), plus the last
    hour's first sends, ready and failures."""
    lim = limits()
    with history.history_engine().begin() as conn:
        classes = [dict(r) for r in conn.execute(text(
            "SELECT " + CLASS_OF + " AS class,t.config->>'source_id' AS source_id,"
            "count(*) FILTER (WHERE p.next_check_at<=clock_timestamp() AND (p.lease_until IS NULL OR "
            "p.lease_until<clock_timestamp())) AS due,"
            "count(*) FILTER (WHERE p.lease_until>=clock_timestamp()) AS leased,"
            "EXTRACT(EPOCH FROM clock_timestamp()-min(p.next_check_at) FILTER (WHERE p.next_check_at<=clock_timestamp()"
            " AND (p.lease_until IS NULL OR p.lease_until<clock_timestamp()))) AS oldest_due_seconds,"
            "max(c.last_claimed_at) AS last_claimed_at "
            "FROM index_publications p JOIN index_targets t ON t.id=p.target_id "
            "LEFT JOIN index_claim_cursor c ON c.source_id=t.config->>'source_id' AND c.class=(" + CLASS_OF + ") "
            "GROUP BY 1,2 ORDER BY 1,2")).mappings()]
        kbs = sorted({r for (r,) in conn.execute(text(
            "SELECT DISTINCT manifest->>'knowledge_base_id' FROM index_publications"))} - {None})
        budget = []
        for kb in kbs:
            n, c, b, unknown = _inflight(conn, kb)
            tokens = conn.execute(text(
                "SELECT LEAST(:burst,tokens+EXTRACT(EPOCH FROM clock_timestamp()-refilled_at)*:rate/60.0) "
                "FROM index_send_bucket WHERE knowledge_base_id=:kb"
            ), {"kb": kb, "burst": lim["burst"], "rate": lim["uploads_per_minute"]}).scalar()
            gates = {r["reason"]: {"denials": r["denials"], "blocked_seconds": round(float(r["blocked"]), 1),
                                   "closed_since": r["closed_since"].isoformat() if r["closed_since"] else None,
                                   "last_at": r["last_at"].isoformat() if r["last_at"] else None}
                     for r in conn.execute(text(
                         "SELECT reason,denials,last_at,closed_since,blocked_seconds+COALESCE(EXTRACT(EPOCH FROM "
                         "clock_timestamp()-closed_since),0) AS blocked FROM index_gate_stats WHERE knowledge_base_id=:kb"
                     ), {"kb": kb}).mappings()}
            budget.append({"knowledge_base_id": kb, "inflight": {"entries": n, "chars": int(c), "bytes": int(b),
                                                                 "unknown_size_entries": unknown},
                           "send_tokens": None if tokens is None else round(float(tokens), 2), "gate": gates,
                           "uploaded": _uploaded(conn, kb)})
        hour = conn.execute(text(
            "SELECT count(*) FILTER (WHERE first_send_at>clock_timestamp()-interval '1 hour') AS first_sends,"
            "count(*) FILTER (WHERE ready_at>clock_timestamp()-interval '1 hour') AS ready,"
            "count(*) FILTER (WHERE error IS NOT NULL AND updated_at>clock_timestamp()-interval '1 hour') AS failed "
            "FROM index_publications")).mappings().one()
    for r in classes:
        r["oldest_due_seconds"] = None if r["oldest_due_seconds"] is None else round(float(r["oldest_due_seconds"]), 1)
        r["last_claimed_at"] = r["last_claimed_at"].isoformat() if r["last_claimed_at"] else None
    return {"rotation": list(ROTATION), "rotation_gaps": rotation_gaps(), "limits": lim, "classes": classes,
            "knowledge_bases": budget,
            "last_hour": dict(hour)}


def never_sent(row):
    """确认从没发出过：首次发送时间、远端知识编号、回执都没有，状态是 queued，或是因此在本地停用的 disabled
    （之后每次复查仍不联网）。改造前有 ready 而 first_send_at 为空的行、发出后丢了回执的
    waiting / sending 行都不算，一律先向桥接对账。"""
    return (row["first_send_at"] is None and not row["knowledge_id"] and not row["receipt"]
            and row["state"] in ("queued", "disabled"))


def _current_state(row):
    """这条索引还该不该在：来源撤权或目标停用 → revoked；这份正文仍是该记录某个版本的当前产物 → active；
    不再是了（处理器升级、改造前的投影待办） → 替代产物都 ready 后 superseded，之前 retiring（保留、不上传）。更正、撤回不在这里停用——历史版本仍要能被旧截止时间检索到，
    此刻能不能返回由检索时的校验按模式和截止时间判定（db.retrieval）。"""
    target = row["config"]
    with history.history_engine().begin() as conn:
        enabled = conn.execute(
            text("SELECT enabled FROM index_targets WHERE id=:id"), {"id": row["target_id"]}
        ).scalar_one()
        if not enabled:
            return "revoked"
        try:
            history._source(conn, target["source_id"])
        except HistoryError as exc:
            if exc.status == 403:
                return "revoked"
            raise
        versions = [v for (v,) in conn.execute(text(
            "SELECT version_id FROM " + history._VERSIONS + " "
            "WHERE source_id=:source AND is_demo=:demo AND source_record_id=:native "
            "AND " + history._ROLE + "!='context_reference'"
        ), {"source": target["source_id"], "demo": target["is_demo"], "native": row["source_record_id"]})]
        current = {artifact_key(a): a for a in current_artifacts(conn, versions).values()}
        mine = current.get(row["artifact_key"])
        if mine is not None:
            # 产物是当前的，还要这一片属于当前分片计划：整篇条目而现在要分片、分片规则或范围
            # 已换的片，由按当前计划建的片替代。没发出过或没 ready 过的直接 superseded（超预算的整篇永远到不了
            # ready）；已经 ready 的等当前计划的整套片全部 ready 再停用（先上后下）
            if row["piece_key"] in index_pieces.keys(mine["text"], mine.get("segments") or []):
                return "active"
            if never_sent(row) or row["state"] != "ready":
                return "superseded"
            replacements = [mine]
        else:
            # 这份正文（处理器升级前的产物，或改造前按投影正文建的待办）已不是任何版本的当前产物。
            # 没发出过的直接停用；发出过的，等该记录的当前产物都整份可检索后再停用，不撤掉最后的入口；
            # 在那之前原样保留、不再上传（retiring）。检索时照样逐条校验。
            if never_sent(row):
                return "superseded"
            replacements = list(current.values())
            if not replacements:
                # 还没有任何当前产物可替代它（新版本的清洗或画像还没做完，例如画像处理器刚升级）：不撤掉最后的
                # 入口；“全部替代产物已 ready”在一个替代都没有时不能算成立
                return "retiring"
        done = all(artifact_ready(conn, row["target_id"], row["source_record_id"], a) for a in replacements)
        return "superseded" if done else "retiring"


def _sent(row):
    with history.history_engine().begin() as conn:
        if JOBS.update(conn, row, "first_send_at=COALESCE(first_send_at,clock_timestamp()),"
                       "state='sending',updated_at=clock_timestamp()") != 1:
            raise HistoryError("index_lease_lost", "索引处理资格已改变", 409)


def _validate_receipt(row, receipt):
    expected = row["manifest"]
    if (
        receipt.get("is_demo", False) is not row.get("config", {}).get("is_demo", expected.get("is_demo", False))
        or receipt.get("publication_id") != row["remote_id"]
        or receipt.get("knowledge_base_id") != expected["knowledge_base_id"]
        or receipt.get("source_scope") != expected["source_scope"]
        or receipt.get("content_sha256") != expected["content_sha256"]
        or any((receipt.get("identity") or {}).get(k) != expected[k]
               for k in ["source_instance", "source_id", "source_record_id", "version_id", "raw_sha256"])
        or receipt.get("raw_locator") != expected["raw_locator"]
        or (receipt.get("identity") or {}).get("artifact_key") != expected.get("artifact_key")
        or (receipt.get("identity") or {}).get("piece") != expected.get("piece")
    ):
        raise HistoryError("index_binding_mismatch", "索引回执与固定原件不符", 409)


def _finish(row, state, desired, *, receipt=None, error=None, delay=30, final=False):
    """final：重试也不会好（超出上传或分块核验预算）——直接受阻，记明原因和覆盖缺口，不进入无休止的 indexing。"""
    attempts = max(row["attempts"] + 1, 6 if final else 0) if error else 0
    if error:
        delay = backoff(attempts, base=10, cap=3600)
        if attempts >= 6:
            state = "blocked"
            if desired != "active":
                delay = 3600  # 清理失败每小时核对一次；不再上传资料。
    with history.history_engine().begin() as conn:
        released = JOBS.release(conn, row, """state=:state,desired_state=:desired,
              receipt=COALESCE(CAST(:receipt AS jsonb),receipt),
              knowledge_id=COALESCE(:knowledge,knowledge_id),error=CAST(:error AS jsonb),attempts=:attempts,
              ready_at=CASE WHEN :state='ready' THEN COALESCE(ready_at,clock_timestamp()) ELSE ready_at END,
              next_check_at=clock_timestamp()+make_interval(secs=>:delay),updated_at=clock_timestamp()""",
            {"state": state, "desired": desired, "receipt": canonical(receipt) if receipt is not None else None,
             "knowledge": receipt.get("knowledge_id") if receipt else None,
             "error": canonical(error) if error else None, "attempts": attempts, "delay": delay})
        if released != 1:
            raise HistoryError("index_lease_lost", "迟到索引回执未覆盖当前处理资格", 409)
        if state == "ready" and row["state"] != "ready":
            # 刚能检索：同一记录里等着交接的其他产物的条目马上复查（同一产物的其他片不用叫醒）
            conn.execute(text(
                "UPDATE index_publications SET next_check_at=clock_timestamp() WHERE target_id=:t "
                "AND source_record_id=:n AND artifact_key<>:k AND state<>'disabled'"
            ), {"t": row["target_id"], "n": row["source_record_id"], "k": row["artifact_key"]})
    return {"id": row["id"], "state": state, "desired_state": desired}


def _hold(row, seconds):
    """不联网、不改状态，过一会儿再看（受阻作业等权限/头版变化；改造前的待办等新产物 ready）。"""
    with history.history_engine().begin() as conn:
        if JOBS.release(conn, row, "next_check_at=clock_timestamp()+make_interval(secs=>:seconds)",
                        {"seconds": seconds}) != 1:
            raise HistoryError("index_lease_lost", "索引处理资格已改变", 409)
    return {"id": row["id"], "state": row["state"], "desired_state": row["desired_state"]}


def process_once(*, client=None, classes=None, lane=None, sent=None):
    """处理一个到期作业。默认按作业类别（T-30：classes 依次尝试，空的让给下一类）；lane/sent 仍可显式指定
    （旧调用，先在那一类里找，没有就退到同 lane，再退到不限）。"""
    row = _claim_next(classes, lane, sent)
    if row is None:
        return {"state": "idle"}
    attempt = _Attempt(row, client)
    owned = client is None
    try:
        return _process_job(attempt)
    except HistoryError as exc:
        if exc.code == "index_lease_lost":
            return {"id": row["id"], "state": "lease_lost"}
        return _finish(row, "waiting", attempt.desired, receipt=attempt.verified_receipt,
                       error={"code": exc.code, "message": exc.message})
    except Exception:
        return _finish(row, "waiting", attempt.desired, receipt=attempt.verified_receipt,
                       error={"code": "index_unavailable", "message": "索引处理暂不可用；已发送请求保留对账状态"})
    finally:
        if owned and attempt.client is not None:
            attempt.client.close()


def _claim_next(classes, lane, sent):
    """The next due job: by job class in rotation order, or (lane/sent given) in that lane and sent kind,
    then the lane, then any. None when nothing is due."""
    if lane is None and sent is None:
        for klass in dict.fromkeys(classes or ROTATION):
            row = _claim_class(klass)
            if row is not None:
                return row
        return None
    for want in dict.fromkeys(((lane, sent), (lane, None), (None, None))):
        row = _claim(*want)
        if row is not None:
            return row
    return None


class _Attempt:
    """One processing of a claimed job. The failure handling of process_once records what the steps had
    reached: the state the job should be in (desired), the last receipt that passed validation, and the
    bridge client once one is in use."""

    def __init__(self, row, client):
        self.row, self.client = row, client
        self.desired = "unknown"
        self.verified_receipt = None


def _process_job(attempt):
    """The steps of one job: decide the wanted state, settle what needs no network, then read the bridge's
    receipt and either disable the entry or publish it."""
    row = attempt.row
    attempt.desired = desired = _current_state(row)
    if "is_demo" in row["manifest"] and row["manifest"]["is_demo"] is not row["config"]["is_demo"]:
        raise HistoryError("index_source_space_mismatch", "索引意图与目标空间不一致", 409)
    offline = _settle_offline(row, desired)
    if offline is not None:
        return offline
    attempt.client = client = attempt.client or WeKnoraIndexClient()
    receipt = client.status(row["remote_id"])
    if receipt is not None:
        _validate_receipt(row, receipt)
        attempt.verified_receipt = receipt
    if desired != "active":
        return _disable(row, client, receipt, desired)
    return _publish_active(attempt, receipt)


def _settle_offline(row, desired):
    """The outcome reached without calling the bridge, or None to go on. A blocked job whose wanted state
    changed starts counting its attempts again."""
    if desired != "active" and never_sent(row):
        return _finish(row, "disabled", desired, delay=300)  # 确认从没发出过：不联网，直接停用
    if desired == "retiring":
        return _hold(row, 300)  # 等替代产物 ready（先上后下），不联网
    if row["state"] == "blocked":
        if desired == row["desired_state"] and desired == "active":
            return _hold(row, 60)  # 不无限重发失败请求；保留错误，继续检查是否需要清理
        if desired != row["desired_state"]:
            row["attempts"] = 0
    return None


def _disable(row, client, receipt, desired):
    """Withdraw an entry that was sent: only once the bridge knows it, and disabled only when confirmed."""
    if receipt is None:
        raise HistoryError("index_disable_unknown", "曾发送的索引结果未知，尚不能确认停用", 409)
    receipt = client.disable(row["remote_id"], desired)
    _validate_receipt(row, receipt)
    confirmed = (
        receipt.get("source_status") == "disabled"
        and receipt.get("index_status") == receipt.get("vector_status") == "disabled"
        and receipt.get("parse_status") in {"completed", "failed", "cancelled"}
        and receipt.get("vector_count") == 0
    )
    return _finish(row, "disabled" if confirmed else "disabling", desired, receipt=receipt, delay=300 if confirmed else 15)


def _publish_active(attempt, receipt):
    """An entry that should be active: check its content against the frozen manifest, upload it when the bridge
    does not have it bound, then settle on the validated receipt."""
    row, client, desired = attempt.row, attempt.client, attempt.desired
    with history.history_engine().begin() as conn:
        full = _artifact_text(conn, row["version_id"], row["artifact_key"]) or ""
    content = index_pieces.content(full, row["piece"]) if row.get("piece") else full
    if not content.strip():
        raise HistoryError("index_content_empty", "清洗产物为空，原件仍保留", 422)
    if not index_pieces.fits(content):
        # 改造前冻结的整篇清单、或分片规则之外的超长内容：不上传，记缺口（原件与清洗产物都保留）
        return _finish(row, "blocked", desired, final=True, error={
            "code": "index_content_budget", "message": "超过单条上传预算（%d 字、%d 字节），原件与清洗产物保留，"
            "记为覆盖缺口" % (len(content), len(content.encode()))})
    if sha256(content) != row["manifest"]["content_sha256"]:
        raise HistoryError("index_content_changed", "清洗产物与冻结摘要不符", 409)
    if receipt is None or not receipt.get("knowledge_id") or receipt.get("binding_status") != "bound":
        # GET先核对。服务端以同一固定publication_id保留意图并恢复，不能二次裸上传。
        # 桥接还不知道这条（首传），或明确记为建档前拒收（重传）：这才是一次物理上传，无论哪一类作业领到，
        # 都先过同一个原子在途预算和共享发送令牌（T-30）。补绑定、对账找回只是 GET/补投影，不占名额。
        physical = receipt is None or (receipt.get("upload_status") == "rejected" and not receipt.get("knowledge_id"))
        if physical:
            denied = _reserve(row, content, receipt)
            if denied:
                return _hold(row, 15)
        else:
            _sent(row)
        receipt = client.publish(_bridge_body(row, content))
    _validate_receipt(row, receipt)
    attempt.verified_receipt = receipt
    return _settle_published(row, receipt, desired)


def _settle_published(row, receipt, desired):
    """The job's state from a validated publish receipt, after re-checking the wanted state."""
    # 网络请求期间可能撤权/换头版；不能以旧资格确认ready。
    latest = _current_state(row)
    if latest in ("revoked", "superseded"):
        return _finish(row, "disabling", latest, receipt=receipt, delay=0)
    if receipt.get("index_status") == "budget_exceeded":
        # 上游实际分出的块数超过桥接核验预算：按新分片规则重新计划之前不会好，不无休止等待
        return _finish(row, "blocked", desired, receipt=receipt, final=True, error={
            "code": "index_chunk_budget", "message": "实际块数超过核验预算（%s 块），记为覆盖缺口"
            % receipt.get("chunk_count")})
    if receipt.get("source_status") != "active":
        if (receipt.get("source_change_type") == "disabled_upstream"
                and receipt.get("parse_status") in {"pending", "processing", "finalizing"}):
            return _finish(row, "indexing", "active", receipt=receipt, delay=15)
        raise HistoryError("index_upstream_disabled", "索引已停用，须明确恢复授权后再启用", 409)
    ready = (
        bool(receipt.get("knowledge_id")) and receipt.get("upload_status") == "uploaded"
        and receipt.get("parse_status") in {"completed", "finalizing"}
        and receipt.get("vector_status") == receipt.get("index_status") == "ready"
        and receipt.get("binding_status") == "bound"
    )
    return _finish(row, "ready" if ready else "indexing", "active", receipt=receipt, delay=60 if ready else 15)


def configured():
    """有索引目标配置，或库里还有目标（配置移除＝停止使用，旧绑定仍要撤回对账）。"""
    if os.environ.get("DATAFABRIC_INDEX_TARGETS"):
        return True
    if not os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL"):
        return False
    with history.history_engine().begin() as conn:
        if conn.execute(text("SELECT to_regclass('index_targets')")).scalar_one() is None:
            return False
        return conn.execute(text("SELECT EXISTS(SELECT 1 FROM index_targets)")).scalar_one()


def run_worker(*, concurrency=2, scan_every_seconds=30.0, idle_seconds=5.0, stop=None, rounds=None):
    """独立的索引 worker：concurrency 个线程各自认领、处理；线程 0 每 scan_every_seconds 做一次回填扫描。
    stop（threading.Event）置位或跑满 rounds 轮后返回各线程的处理计数。"""
    if not 1 <= concurrency <= 32:
        raise HistoryError("index_budget", "索引并发须为1至32")
    targets()  # a configuration naming a retired source stops the worker here, not at the first search
    stop = stop or threading.Event()
    counts = [dict() for _ in range(concurrency)]

    def loop(slot):
        last_scan, n = 0.0, 0
        while not stop.is_set() and (rounds is None or n < rounds):
            n += 1
            try:
                if slot == 0 and time.monotonic() - last_scan >= scan_every_seconds:
                    last_scan = time.monotonic()
                    scan_committed()
                turn = (slot + n) % len(ROTATION)
                state = process_once(classes=ROTATION[turn:] + ROTATION[:turn])["state"]
            except Exception:  # noqa: BLE001 -- 库或配置暂不可用：待办与水位都在库里，稍后再来
                state = "unavailable"
            counts[slot][state] = counts[slot].get(state, 0) + 1
            if state in {"idle", "unavailable"}:
                stop.wait(idle_seconds)

    threads = [threading.Thread(target=loop, args=(slot,), name="index-%d" % slot, daemon=True)
               for slot in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return counts

# index_publications.state 的全部取值（流水线总览和管理页按这个顺序显示）
STATES = ("queued", "sending", "indexing", "ready", "waiting", "blocked", "disabling", "disabled")
_JOB_ID = re.compile(r"[0-9a-f]{64}")


def _job_id(key):
    if not isinstance(key, str) or not _JOB_ID.fullmatch(key):
        raise HistoryError("index_publication_id", "索引作业编号须为64位小写十六进制", 422)
    return key


def _cursor(value):
    """续页标记是上一页最后一条的“更新时间|作业编号”；格式不对就拒绝，不猜。"""
    try:
        stamp, key = value.rsplit("|", 1)
        moment = parse_timestamp(stamp)
        if moment.tzinfo is None:
            raise ValueError()
        return moment, _job_id(key)
    except (ValueError, AttributeError, HistoryError):
        raise HistoryError("index_cursor", "索引列表续页标记无效", 422) from None


def status(*, limit=100, state=None, source_id=None, cursor=None):
    """索引作业列表：按更新时间倒序，可按状态、来源过滤；next_cursor 不为空时还有下一页。"""
    if not 1 <= limit <= 100:
        raise HistoryError("index_budget", "索引状态最多读取100项")
    if state is not None and state not in STATES:
        raise HistoryError("index_filter", "索引状态过滤值无效", 422)
    if source_id is not None and not 1 <= len(source_id) <= 80:
        raise HistoryError("index_filter", "来源编号过滤值无效", 422)
    where, params = [], {"limit": limit + 1}
    if state is not None:
        where.append("p.state=:state")
        params["state"] = state
    if source_id is not None:
        where.append("t.config->>'source_id'=:source")
        params["source"] = source_id
    if cursor is not None:
        params["before_at"], params["before_id"] = _cursor(cursor)
        where.append("(p.updated_at,p.id)<(:before_at,:before_id)")
    with history.history_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT p.*,t.config FROM index_publications p JOIN index_targets t ON t.id=p.target_id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY p.updated_at DESC,p.id DESC LIMIT :limit"
        ), params).mappings().all()
        more = len(rows) > limit
        rows = rows[:limit]
        result = []
        for row in rows:
            permitted = True
            try:
                history._source(conn, row["config"]["source_id"])
            except HistoryError as exc:
                if exc.status != 403:
                    raise
                permitted = False
            result.append({k: row[k] for k in [
                "id","target_id","version_id","source_record_id","remote_id","state","desired_state",
                "knowledge_id","receipt","error","attempts","first_send_at","next_check_at","updated_at",
            ]} | {"artifact_key": row.get("artifact_key") or "", "lane": row.get("lane"),
                  "piece_index": row.get("piece_index"), "piece": row.get("piece"),
                  "created_at": row.get("created_at"), "ready_at": row.get("ready_at"),
                  "title": (row["manifest"] or {}).get("title"),
                  "source": row["config"], "read_permitted": permitted})
        last = rows[-1] if more else None
        return {"items": result,
                "next_cursor": last["updated_at"].isoformat() + "|" + last["id"] if last else None}


def state_counts():
    """各索引发布状态的条数（流水线总览用）。"""
    with history.history_engine().begin() as conn:
        return {r[0]: r[1] for r in conn.execute(text(
            "SELECT state, count(*) FROM index_publications GROUP BY state"))}


def retry(key):
    """把受阻（blocked）的作业放回待重试：尝试次数清零、立即到期，由索引 worker 处理；
    处理时仍先向桥接服务核对已发请求，不会二次裸上传。其余状态不动，如实返回当前状态。"""
    key = _job_id(key)
    with history.history_engine().begin() as conn:
        result = conn.execute(text("""
            UPDATE index_publications SET state='waiting',attempts=0,next_check_at=clock_timestamp(),
              updated_at=clock_timestamp()
            WHERE id=:id AND state='blocked' AND (lease_until IS NULL OR lease_until<clock_timestamp())
        """), {"id": key})
        if result.rowcount == 1:
            return {"id": key, "scheduled": True, "state": "waiting"}
        row = conn.execute(text(
            "SELECT state,(lease_until IS NOT NULL AND lease_until>=clock_timestamp()) AS leased "
            "FROM index_publications WHERE id=:id"
        ), {"id": key}).mappings().first()
        if row is None:
            raise HistoryError("index_publication_not_found", "没有这条索引作业", 404)
        reason = "leased" if row["state"] == "blocked" and row["leased"] else "not_blocked"
        return {"id": key, "scheduled": False, "state": row["state"], "reason": reason}
