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
from uuid import uuid4

from pydantic import Field
from sqlalchemy import text

from biz_rules.history_models import (
    HistoryError,
    HistoryRecord,
    Model,
    canonical,
    sha256,
)
from core_engine.weknora_index_client import WeKnoraIndexClient, publication_id
from biz_rules.observations import OBSERVATION_PARSER
from db import history

FRESH_DAYS = 3
MAX_IDENTIFIERS = 50


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
        return result
    except (ValueError, TypeError):
        raise HistoryError("index_target_configuration", "来源到知识库的索引配置无效", 503) from None


def _text_processors():
    from processing import attachment_parse, clean_text

    return [clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION]


# 一批版本的当前产物：当前画像处理器的画像 + 它所基于的那份正文（同一处理器版本下最新一次成功结果）
_CURRENT = """
SELECT DISTINCT ON (p.version_id) p.version_id,p.doc_type,p.doc_category,p.evidence_types,
       c.processor_version,c.text,c.text_sha256,c.locator_kind
FROM derived_profiles p
JOIN LATERAL (SELECT processor_version,text,text_sha256,locator_kind FROM derived_clean_text
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


def metadata(record, proof_available_at, artifact, identifiers):
    """检索元数据（桥接服务写进 WeKnora，命中原样带回）。值都是字符串或字符串列表；没有的不写。
    时间：published_at / source_updated_at 是来源报告的（精度见值本身：年、年-月、日期或时刻），
    first_seen_at 是 Fabric 首次见到，proof_available_at 是有证明的可得时间（只有审核过的档案才有）。"""
    proof = record.availability_proof
    values = {
        "source_id": record.source_id, "source_record_id": record.source_record_id,
        "version_id": record.version_id,
        "published_at": _time(record.source_published_at), "source_updated_at": _time(record.source_updated_at),
        "first_seen_at": record.first_seen_at.isoformat(),
        "proof_available_at": proof_available_at.isoformat() if proof_available_at else None,
        "proof_kind": proof.kind, "proof_uri": proof.uri if proof.kind == "reviewed_archive" else None,
        "doc_type": artifact["doc_type"], "doc_category": artifact["doc_category"],
        "evidence_types": list(artifact["evidence_types"] or []),
        "processor_version": artifact["processor_version"], "artifact_sha256": artifact["text_sha256"],
        "identifiers": identifiers,
    }
    return {k: v for k, v in values.items() if v not in (None, [], "")}


def _manifest(target, record, artifact, meta):
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
        "content_sha256": artifact["text_sha256"],
        # 正文是哪一份产物：命中能回指“版本 + 这份正文”，附件还回指父页面
        "artifact_key": artifact_key(artifact), "processor_version": artifact["processor_version"],
        "artifact_sha256": artifact["text_sha256"], "locator_kind": artifact.get("locator_kind"),
        "parent_record_id": (record.facts or {}).get("parent_record_id"),
        "metadata": meta,
    }


# 桥接服务（extensions/kdd Publication，extra=forbid）认识的字段；Fabric 清单里其余的说明字段只留在本地
# is_demo：演示目标的发布必须带上（桥接按它隔离演示与真实知识库；不带等于真实）
BRIDGE_FIELDS = ("source_instance", "source_id", "source_record_id", "version_id", "raw_sha256", "artifact_key",
                 "is_demo", "knowledge_base_id", "source_scope", "raw_locator", "content_sha256", "metadata")


def _bridge_body(row, content):
    """发给桥接的请求体。is_demo 取自索引目标（权威，不依赖清单是何时冻结的）。"""
    manifest = row["manifest"]
    body = {k: manifest[k] for k in BRIDGE_FIELDS if k in manifest}
    return {**body, "is_demo": bool(row["config"]["is_demo"]), "title": manifest["title"][:255], "content": content}


def _enqueue(conn, target, record, artifact, meta):
    """一条（目标, 记录, 产物）的索引待办：同一记录里清洗正文相同的版本只建一条，代表版本是最先产出这份正文的
    版本。检索时由 Fabric 按模式和截止时间找出此刻可见的版本，它的当前产物就是这份正文才返回（db.retrieval）。
    新建后，同一记录的其他待办尽快复查（处理器升级后旧产物会被判为 superseded 并停用，记录保留）。"""
    key = artifact_key(artifact)
    manifest = _manifest(target.model_dump(), record, artifact, meta)
    lane = "new" if (datetime.now(record.first_seen_at.tzinfo) - record.first_seen_at).days < FRESH_DAYS else "backfill"
    inserted = conn.execute(text("""
        INSERT INTO index_publications
          (id,target_id,version_id,source_record_id,remote_id,manifest,artifact_key,lane)
        SELECT :id,:target,:version,:native,:remote,CAST(:manifest AS jsonb),:key,:lane
        WHERE NOT EXISTS (SELECT 1 FROM index_publications
                          WHERE target_id=:target AND source_record_id=:native AND artifact_key=:key)
        ON CONFLICT(target_id,version_id,artifact_key) DO NOTHING
    """), {"id": sha256(canonical([target.key(), record.version_id, key])), "target": target.key(),
            "version": record.version_id, "native": record.source_record_id,
            "remote": publication_id(manifest), "manifest": canonical(manifest), "key": key, "lane": lane}).rowcount
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
        artifact = artifacts[record.version_id]
        meta = metadata(record, row["proof_available_at"], artifact, found.get(record.version_id, []))
        for target in wanted:
            if (target.source_id, target.is_demo) == (record.source_id, record.is_demo):
                count += _enqueue(conn, target, record, artifact, meta)
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

def _claim(lane=None, sent=None):
    """下一个到期作业。lane=new/backfill、sent=True/False 只在那一类里找（各有部分索引）；None 不限。"""
    where = ""
    if lane is not None:
        where += " AND p.lane=:lane"
    if sent is not None:
        where += " AND p.first_send_at IS NOT NULL" if sent else " AND p.first_send_at IS NULL"
    with history.history_engine().begin() as conn:
        row = conn.execute(text("""
            SELECT p.*,t.config,t.enabled FROM index_publications p
            JOIN index_targets t ON t.id=p.target_id
            WHERE p.next_check_at<=clock_timestamp()
              AND (p.lease_until IS NULL OR p.lease_until<clock_timestamp())""" + where + """
            ORDER BY p.next_check_at,p.id FOR UPDATE OF p SKIP LOCKED LIMIT 1
        """), {"lane": lane}).mappings().first()
        if row is None:
            return None
        token = uuid4().hex
        conn.execute(text("""
            UPDATE index_publications SET lease_token=:token,
              lease_until=clock_timestamp()+interval '120 seconds',
              updated_at=clock_timestamp() WHERE id=:id
        """), {"id": row["id"], "token": token})
        return dict(row, lease_token=token)


def never_sent(row):
    """确认从没发出过：首次发送时间、远端知识编号、回执都没有，状态是 queued，或是因此在本地停用的 disabled
    （之后每次复查仍不联网）。改造前有 ready 而 first_send_at 为空的行（Codex r72）、发出后丢了回执的
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
        keys = {artifact_key(a) for a in current_artifacts(conn, versions).values()}
        if row["artifact_key"] in keys:
            return "active"
        # 这份正文（处理器升级前的产物，或改造前按投影正文建的待办）已不是任何版本的当前产物。
        # 没发出过的直接停用；发出过的，等该记录的当前产物都能检索（ready）后再停用，不撤掉最后的入口；
        # 在那之前原样保留、不再上传（retiring）。检索时照样逐条校验，被更正、撤回的版本不会返回。
        ready = {key for (key,) in conn.execute(text(
            "SELECT artifact_key FROM index_publications WHERE target_id=:t AND source_record_id=:n "
            "AND state='ready' AND artifact_key=ANY(:keys)"
        ), {"t": row["target_id"], "n": row["source_record_id"], "keys": list(keys)})}
        return "superseded" if never_sent(row) or keys <= ready else "retiring"


def _sent(row):
    with history.history_engine().begin() as conn:
        changed = conn.execute(text("""
            UPDATE index_publications SET first_send_at=COALESCE(first_send_at,clock_timestamp()),
              state='sending',updated_at=clock_timestamp()
            WHERE id=:id AND lease_token=:token AND lease_until>clock_timestamp()
        """), {"id": row["id"], "token": row["lease_token"]})
        if changed.rowcount != 1:
            raise HistoryError("index_lease_lost", "索引处理资格已改变", 409)


def _validate_receipt(row, receipt):
    expected = row["manifest"]
    if (
        receipt.get("publication_id") != row["remote_id"]
        or receipt.get("knowledge_base_id") != expected["knowledge_base_id"]
        or receipt.get("source_scope") != expected["source_scope"]
        or receipt.get("content_sha256") != expected["content_sha256"]
        or any((receipt.get("identity") or {}).get(k) != expected[k]
               for k in ["source_instance", "source_id", "source_record_id", "version_id", "raw_sha256"])
        or receipt.get("raw_locator") != expected["raw_locator"]
        or (receipt.get("identity") or {}).get("artifact_key") != expected.get("artifact_key")
    ):
        raise HistoryError("index_binding_mismatch", "索引回执与固定原件不符", 409)


def _finish(row, state, desired, *, receipt=None, error=None, delay=30):
    attempts = row["attempts"] + 1 if error else 0
    if error:
        delay = min(3600, 10 * 2 ** min(attempts, 8))
        if attempts >= 6:
            state = "blocked"
            if desired != "active":
                delay = 3600  # 清理失败每小时核对一次；不再上传资料。
    with history.history_engine().begin() as conn:
        result = conn.execute(text("""
            UPDATE index_publications SET state=:state,desired_state=:desired,
              receipt=COALESCE(CAST(:receipt AS jsonb),receipt),
              knowledge_id=COALESCE(:knowledge,knowledge_id),error=CAST(:error AS jsonb),
              attempts=:attempts,lease_token=NULL,lease_until=NULL,
              ready_at=CASE WHEN :state='ready' THEN COALESCE(ready_at,clock_timestamp()) ELSE ready_at END,
              next_check_at=clock_timestamp()+make_interval(secs=>:delay),updated_at=clock_timestamp()
            WHERE id=:id AND lease_token=:token AND lease_until>clock_timestamp()
        """), {"id": row["id"], "token": row["lease_token"], "state": state,
                "desired": desired, "receipt": canonical(receipt) if receipt is not None else None,
                "knowledge": receipt.get("knowledge_id") if receipt else None,
                "error": canonical(error) if error else None, "attempts": attempts, "delay": delay})
        if result.rowcount != 1:
            raise HistoryError("index_lease_lost", "迟到索引回执未覆盖当前处理资格", 409)
        if state == "ready" and row["state"] != "ready":
            # 刚能检索：同一记录里等着交接的旧条目马上复查
            conn.execute(text(
                "UPDATE index_publications SET next_check_at=clock_timestamp() WHERE target_id=:t "
                "AND source_record_id=:n AND id<>:id AND state<>'disabled'"
            ), {"t": row["target_id"], "n": row["source_record_id"], "id": row["id"]})
    return {"id": row["id"], "state": state, "desired_state": desired}


def _hold(row, seconds):
    """不联网、不改状态，过一会儿再看（受阻作业等权限/头版变化；改造前的待办等新产物 ready）。"""
    with history.history_engine().begin() as conn:
        result = conn.execute(text("""
            UPDATE index_publications SET lease_token=NULL,lease_until=NULL,
              next_check_at=clock_timestamp()+make_interval(secs=>:seconds)
            WHERE id=:id AND lease_token=:token AND lease_until>clock_timestamp()
        """), {"id": row["id"], "token": row["lease_token"], "seconds": seconds})
        if result.rowcount != 1:
            raise HistoryError("index_lease_lost", "索引处理资格已改变", 409)
    return {"id": row["id"], "state": row["state"], "desired_state": row["desired_state"]}


def process_once(*, client=None, lane=None, sent=None):
    """处理一个到期作业。先在（lane, sent）那一类里找，没有就退到同 lane，再退到不限。"""
    row = None
    for want in dict.fromkeys(((lane, sent), (lane, None), (None, None))):
        row = _claim(*want)
        if row is not None:
            break
    if row is None:
        return {"state": "idle"}
    desired = "unknown"
    owned = client is None
    verified_receipt = None
    try:
        desired = _current_state(row)
        if desired != "active" and never_sent(row):
            return _finish(row, "disabled", desired, delay=300)  # 确认从没发出过：不联网，直接停用
        if desired == "retiring":
            return _hold(row, 300)  # 等替代产物 ready（先上后下），不联网
        if row["state"] == "blocked":
            if desired == row["desired_state"] and desired == "active":
                return _hold(row, 60)  # 不无限重发失败请求；保留错误，继续检查是否需要清理
            if desired != row["desired_state"]:
                row["attempts"] = 0
        client = client or WeKnoraIndexClient()
        receipt = client.status(row["remote_id"])
        if receipt is not None:
            _validate_receipt(row, receipt)
            verified_receipt = receipt
        if desired != "active":
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
        with history.history_engine().begin() as conn:
            content = _artifact_text(conn, row["version_id"], row["artifact_key"]) or ""
        if not content.strip() or len(content.encode()) > 2000000:
            raise HistoryError("index_content_budget", "正文为空或超过索引单条预算，原件仍保留", 422)
        if sha256(content) != row["manifest"]["content_sha256"]:
            raise HistoryError("index_content_changed", "清洗产物与冻结摘要不符", 409)
        if receipt is None or not receipt.get("knowledge_id") or receipt.get("binding_status") != "bound":
            _sent(row)
            # GET先核对。服务端以同一固定publication_id保留意图并恢复，不能二次裸上传。
            receipt = client.publish(_bridge_body(row, content))
        _validate_receipt(row, receipt)
        verified_receipt = receipt
        # 网络请求期间可能撤权/换头版；不能以旧资格确认ready。
        latest = _current_state(row)
        if latest in ("revoked", "superseded"):
            return _finish(row, "disabling", latest, receipt=receipt, delay=0)
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
    except HistoryError as exc:
        if exc.code == "index_lease_lost":
            return {"id": row["id"], "state": "lease_lost"}
        return _finish(row, "waiting", desired, receipt=verified_receipt, error={"code": exc.code, "message": exc.message})
    except Exception:
        return _finish(row, "waiting", desired, receipt=verified_receipt, error={"code": "index_unavailable", "message": "索引处理暂不可用；已发送请求保留对账状态"})
    finally:
        if owned and client is not None:
            client.close()


# 一个 worker 线程轮到的（lane, sent）：新增 3 份、回填 1 份；同一 lane 里“确认已发出的”和“发新的”交替。
# 已发出的作业 15 秒后再查，若只按到期时间排，会排在整个积压后面，ready 要等积压清完。
SLOTS = (("new", True), ("new", False), ("new", True), ("backfill", False),
         ("new", False), ("new", True), ("new", False), ("backfill", True))


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
                lane, sent = SLOTS[(slot + n) % len(SLOTS)]
                state = process_once(lane=lane, sent=sent)["state"]
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
        moment = datetime.fromisoformat(stamp)
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
