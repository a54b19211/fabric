"""正文进 WeKnora：清洗正文 → 索引待办（index_publications）→ 桥接服务（本仓 weknora/）→ WeKnora。

从哪读起：run_worker() 是常驻循环，一轮做两件事：scan_committed() 补扫待办，work_once() 领一批到期待办逐条处理。
- 待办：一条 = (索引目标, 记录, 一份清洗正文[, 太长时的一片])。同一记录里清洗正文相同的版本只建一条，代表版本是最先
  产出这份正文的版本；处理器升级后正文没变的也还是这一条（same_text），不重复上传。进知识库的是 index_body：
  清洗正文去掉标了 index=False 的尾部。目标可以带 limit（抽样上限）；LIST_ROW_SOURCES 的列表行不进。入队只有 enqueue_versions：清洗段写画像时同一事务调用；scan_committed 按 published_seq 水位
  补扫已画像而还没有待办的版本（回填、新加的目标）。ON CONFLICT 去重，两条路不会重复。
- 处理（_handle）：先判这条该不该在（_wanted）。该在：桥接还没绑定就上传，按回执记下 knowledge_id、是否 ready；
  不该在：请桥接撤下。出错记下原因、退避重试，连续 MAX_ATTEMPTS 次后停在 blocked（管理页可以放回重试）。
- 历史版本不因“现在不是头版”撤下：更正、撤回之后，检索时由 Fabric 按模式和截止时间核对此刻可见的版本
  （store.retrieval）。只有两种情况撤下：来源撤权或目标停用（revoked）；处理器升级后这份正文已不是该记录任何版本的
  当前产物（superseded，等替代它的正文整份 ready 后再撤，不撤掉最后的入口）。
- 清单（manifest）就是发给桥接的请求体（不含正文），入队时冻结；正文在发送时按 artifact_key 从 derived_clean_text 取。
"""

import json
import os
import re
import threading
import time
from datetime import datetime

from pydantic import Field
from sqlalchemy import text

from store import index_pieces, records
from store.record_models import FabricError, Model, VersionRecord, canonical, sha256
from store.timestamps import parse_timestamp
from store.weknora_index_client import WeKnoraIndexClient, publication_id

OBSERVATION_PARSER = "datafabric.observation.projection.v0.1"  # 旧的观测记录（已不再产生）不进知识库
FRESH_DAYS = 3  # first_seen 在这几天内、来源自己的版本算新增（lane=new），比回填先上传
MAX_IDENTIFIERS = 50
TOMBSTONES = ("withdrawn", "deleted", "retracted")
BATCH = 20  # 一轮领几条
CLAIM_SECONDS = 600  # 领到的条目先推后这么久：进程中途退出，它们 10 分钟后自动重新到期
MAX_ATTEMPTS = 6  # 连续出错这么多次就停（blocked）
POLL_SECONDS = 15  # 桥接还在解析、或还没停用完：过 15 秒再查
HOLD_SECONDS = 300  # 等替代它的正文 ready：5 分钟后再看
RECHECK_SECONDS = 86400  # ready 的每天复查一次（来源撤权、目标停用时撤下）
PARSING = {"pending", "processing", "finalizing"}
PARSE_ENDED = {"completed", "failed", "cancelled"}
MAX_TARGETS = 32  # 索引目标（来源 → 知识库）的个数上限：十个知识库、二十几个来源
# 这些来源的记录本身只是列表里的一行（标题、日期、文件链接），内容全在附件 PDF 里：只有附件的正文进知识库
LIST_ROW_SOURCES = frozenset({"hkex", "hkex_reports"})


class IndexTarget(Model):
    source_instance: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=80)
    is_demo: bool
    knowledge_base_id: str = Field(min_length=1, max_length=64)
    source_scope: str = Field(min_length=1, max_length=128)
    # 抽样：这个目标最多收这么多条记录（已经收了的记录出新版本照常更新）；不写 = 整个来源都收。不算进目标的身份：
    # 以后去掉 limit 还是同一个目标，已收的留着；去掉后把 index_targets.scanned_seq 置 0，补扫当时没收的
    limit: int | None = Field(default=None, ge=1)

    def key(self):
        return sha256(canonical(self.model_dump(exclude={"limit"})))


def targets():
    try:
        values = json.loads(os.environ.get("DATAFABRIC_INDEX_TARGETS") or "[]")
        if not isinstance(values, list) or len(values) > MAX_TARGETS:
            raise ValueError()
        result = [IndexTarget.model_validate(item) for item in values]
        if len({item.key() for item in result}) != len(result):
            raise ValueError()
    except (ValueError, TypeError):
        raise FabricError("index_target_configuration", "来源到知识库的索引配置无效", 503) from None
    from fetch.catalog import RETIRED

    retired = sorted({item.source_id for item in result if item.source_id in RETIRED})
    if retired:
        # a configuration error, not something to filter at search time: it is refused when the service or the worker
        # starts (api.main, run_worker) and by every caller of targets()
        raise FabricError("index_target_retired_source",
                           "索引目标配置了已退役来源 " + ",".join(retired) + "；退役来源不能再作索引目标", 503)
    return result


def _text_processors():
    from clean import attachment_parse, clean_text

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
    from clean import profile

    if not version_ids:
        return {}
    found = {row["version_id"]: dict(row) for row in conn.execute(text(_CURRENT), {
        "ids": list(version_ids), "profile": profile.PROCESSOR_VERSION, "texts": _text_processors()}).mappings()}
    for artifact in found.values():
        artifact["body"] = index_body(artifact["text"], artifact["segments"])
        if artifact["body"] != artifact["text"]:
            artifact["body_sha256"] = sha256(artifact["body"])
    return found


def index_body(text_, segments):
    """进知识库的正文：清洗正文到第一个标了 index=False 的段为止（ClinicalTrials 排在最后的研究地点和结果表，
    clean_text.REGISTRY_TAIL）；没有这样的段就是整份。它是清洗正文的开头一截，命中块照样能在清洗正文里定位。"""
    cut = min((s["start"] for s in segments or [] if s.get("index") is False), default=None)
    return text_ if cut is None else text_[:cut].rstrip()


def artifact_key(artifact):
    """处理器版本:正文摘要。摘要是进知识库的那一截（index_body）的：同一条记录里只在不进知识库的尾部（地点、结果表）
    有差别的几个版本是同一份，只建一条、只上传一次；没有这种尾部的来源，它就是整份清洗正文的摘要。"""
    return artifact["processor_version"] + ":" + artifact.get("body_sha256", artifact["text_sha256"])


def same_text(key, other):
    """两个产物键是不是同一份正文（摘要相同）。处理器升级后正文没变的，键里的处理器版本不同，正文还是那一份：
    它们共用一条索引条目，不再上传第二遍（内容一模一样的文件 WeKnora 也不收）。"""
    return bool(key) and bool(other) and key.rsplit(":", 1)[-1] == other.rsplit(":", 1)[-1]


def artifact_row(conn, version_id, key):
    """这个版本名下、摘要对得上 key 的那份清洗正文 {text, segments, locator_kind, body}；找不到返回 None。
    先看 key 写的那个处理器版本产出的，再看这个版本别的处理器版本产出的（正文相同就是同一份）。"""
    processor, digest = key.rsplit(":", 1)
    for row in conn.execute(text(
        "SELECT text,segments,locator_kind,text_sha256 FROM derived_clean_text WHERE version_id=:v "
        "AND status='parsed' ORDER BY (processor_version=:pv) DESC, created_at DESC, attempt DESC"
    ), {"v": version_id, "pv": processor}).mappings():
        body = index_body(row["text"], row["segments"])
        if digest == row["text_sha256"] or (body != row["text"] and digest == sha256(body)):
            return {**row, "body": body}
    return None


def artifact_ready(conn, target_id, record_id, artifact):
    """这份产物整份可检索：按当前分片计划应有的每一片（不分片即整篇一条）都有 ready 条目。按完整的预期片集合
    判定，数据库里只有其中几片、而这几片都 ready 不算。worker 的交接和检索的交接用同一个判定。"""
    expected = index_pieces.keys(artifact["body"], artifact.get("segments") or [])
    ready = {k for (k,) in conn.execute(text(
        "SELECT piece_key FROM index_publications WHERE target_id=:t AND source_record_id=:n "
        "AND right(artifact_key,64)=:d AND state='ready'"
    ), {"t": target_id, "n": record_id, "d": artifact_key(artifact).rsplit(":", 1)[-1]})}
    return expected <= ready


def _artifact_text(conn, version_id, key):
    """待办绑定的那一份正文（处理器版本:摘要）里进知识库的部分（index_body）；找不到返回 None。"""
    row = artifact_row(conn, version_id, key)
    return row["body"] if row else None


def _identifiers(conn, artifacts):
    """各版本在这份正文上抽到的编号（kind:value，去重、有上限）；编号宁缺不错，缺了以后按血缘补。"""
    from clean import identifiers

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


# 桥接（weknora/models.py 的 Publication，extra=forbid）认识的字段。清单就是请求体（发送时加上 content）；
# 这次改动之前入队的清单里还多几项只留在本地的说明字段，发送时按这个名单滤掉
BRIDGE_FIELDS = ("source_instance", "source_id", "source_record_id", "version_id", "raw_sha256", "artifact_key",
                 "piece", "is_demo", "knowledge_base_id", "source_scope", "raw_locator", "content_sha256", "metadata",
                 "title")


def _manifest(target, record, artifact, meta, piece=None):
    """发给桥接的请求体（正文 content 发送时再加），入队时冻结在 index_publications.manifest。"""
    locator = {"source_instance": target.source_instance, "source_id": record.source_id,
               "source_record_id": record.source_record_id, "version_id": record.version_id}
    return {
        **locator, "raw_sha256": record.raw_sha256, "raw_locator": dict(locator),
        "knowledge_base_id": target.knowledge_base_id, "is_demo": target.is_demo, "source_scope": target.source_scope,
        "title": (record.title or record.source_record_id)[:255],
        # 实际上传的正文：不分片时就是整份产物；分片时是这一片（上下文前缀 + 全篇里的一段）
        "content_sha256": piece["sha256"] if piece else artifact.get("body_sha256", artifact["text_sha256"]),
        **({"piece": piece} if piece else {}),
        "artifact_key": artifact_key(artifact), "metadata": meta,
    }


def _body(row, content):
    return {**{k: v for k, v in row["manifest"].items() if k in BRIDGE_FIELDS}, "content": content}


def _lane(record, now):
    """new：来源自己刚抓到的版本；backfill：其余。旧库迁入的版本（migrated_record）不管哪天导入都是存量——
    2026-11-16 有 36 万条迁入的临床登记排在 new 里，当天抓到的 WHO 公告只能跟在它们后面。"""
    fresh = (now - record.first_seen_at).days < FRESH_DAYS
    return "new" if fresh and (record.facts or {}).get("publication_role") != "migrated_record" else "backfill"


def _enqueue(conn, target, record, artifact, meta, pieces):
    """一条（目标, 记录, 产物）的索引待办：同一记录里清洗正文相同的版本只建一条，代表版本是最先产出这份正文的
    版本。复用键 (目标, 记录, 产物, 片) 由数据库唯一索引保证（V028）：两个事务同时入队同一份正文时，后一个什么也不做。
    太长的产物分片（store.index_pieces）：一片一个物理条目；一次放得下的不分片。
    新建后，同一记录的其他待办尽快复查（处理器升级后旧产物会被判为 superseded 并撤下，记录保留）。"""
    key = artifact_key(artifact)
    lane = _lane(record, datetime.now(record.first_seen_at.tzinfo))
    inserted = 0
    for piece in pieces or [None]:
        manifest = _manifest(target, record, artifact, meta, piece)
        piece_key = index_pieces.key(piece)
        if conn.execute(text(  # 这条记录的这份正文（这一片）已经有条目了，哪怕是别的处理器版本产出的：不再建
            "SELECT 1 FROM index_publications WHERE target_id=:t AND source_record_id=:n AND piece_key=:p "
            "AND right(artifact_key,64)=:d AND state<>'disabled' LIMIT 1"
        ), {"t": target.key(), "n": record.source_record_id, "p": piece_key, "d": key.rsplit(":", 1)[-1]}).first():
            continue
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
    artifacts = {v: a for v, a in current_artifacts(conn, version_ids).items() if a["body"].strip()}
    if not artifacts:
        return 0
    found = _identifiers(conn, artifacts)
    count = 0
    # 图谱和观测投影不进正文索引（观测另有依据可见性检查）；按版本先后入队：同一记录正文相同的几个版本，
    # 代表版本是最早的那个
    for row in conn.execute(text(
        "SELECT payload,proof_available_at FROM fabric_versions WHERE version_id=ANY(:ids) "
        "AND record_kind<>'graph' AND payload->>'parser_version' IS DISTINCT FROM :observation ORDER BY published_seq"
    ), {"ids": list(artifacts), "observation": OBSERVATION_PARSER}).mappings():
        record = VersionRecord.model_validate(row["payload"])
        if not _indexable(record):
            continue
        artifact = artifacts[record.version_id]
        meta = metadata(record, row["proof_available_at"], artifact, found.get(record.version_id, []))
        pieces = index_pieces.plan(artifact["body"], artifact.get("segments") or [])
        for target in wanted:
            if (target.source_id, target.is_demo) == (record.source_id, record.is_demo) and _admitted(
                    conn, target, record.source_record_id):
                count += _enqueue(conn, target, record, artifact, meta, pieces)
    return count


def _indexable(record):
    """这个版本有没有该进知识库的正文。撤回/删除的墓碑版本没有可检索的内容（检索时按模式和截止时间判 withdrawn）；
    LIST_ROW_SOURCES 的列表行没有正文，不占知识库的位置，它的附件（公告、报告的 PDF）解析出正文后照常进。"""
    facts = record.facts or {}
    if facts.get("publication_state") in TOMBSTONES:
        return False
    return record.source_id not in LIST_ROW_SOURCES or facts.get("role") == "attachment"


def _records(conn, target):
    """这个目标现在收了几条记录（撤下的不算）。"""
    return conn.execute(text("SELECT count(DISTINCT source_record_id) FROM index_publications WHERE target_id=:t "
                             "AND state<>'disabled'"), {"t": target.key()}).scalar_one()


def _admitted(conn, target, record_id):
    """抽样上限（IndexTarget.limit）：没写上限、这条记录已经收了、或者还没收满，才收。"""
    if target.limit is None:
        return True
    if conn.execute(text("SELECT 1 FROM index_publications WHERE target_id=:t AND source_record_id=:n "
                         "AND state<>'disabled' LIMIT 1"), {"t": target.key(), "n": record_id}).first():
        return True
    return _records(conn, target) < target.limit


def _readable(conn, source_id):
    """来源此刻可读（许可与开关）；撤权的返回 False，其他错误照常抛出。"""
    try:
        records._source(conn, source_id)
    except FabricError as exc:
        if exc.status == 403:
            return False
        raise
    return True


def _sync_targets(conn, configured):
    for target in configured:
        conn.execute(text("""
            INSERT INTO index_targets(id,config) VALUES (:id,CAST(:config AS jsonb))
            ON CONFLICT(id) DO UPDATE SET enabled=true,config=EXCLUDED.config
        """), {"id": target.key(), "config": target.model_dump_json()})
    conn.execute(
        text("UPDATE index_targets SET enabled=false WHERE NOT(id=ANY(:ids))"),
        {"ids": [item.key() for item in configured]},
    )


def scan_committed(*, batch_size=100):
    """回填扫描：按 published_seq 水位，把已画像而还没有待办的版本放进待办；落待办和推进水位在同一事务。
    还没画像的版本跳过——它们画像时由清洗段同一事务入队。"""
    if not 1 <= batch_size <= 1000:
        raise FabricError("index_budget", "索引扫描批次须为1至1000")
    configured = targets()
    count = 0
    with records.fabric_engine().begin() as conn:
        _sync_targets(conn, configured)
        for target in configured:
            row = conn.execute(
                text("SELECT * FROM index_targets WHERE id=:id FOR UPDATE SKIP LOCKED"),
                {"id": target.key()},
            ).mappings().first()
            if row is None or not _readable(conn, target.source_id):
                continue
            if target.limit is not None and _records(conn, target) >= target.limit:
                continue  # 抽样收满了：不扫，水位也不动
            versions = conn.execute(text("""
                SELECT version_id,published_seq FROM fabric_versions
                WHERE source_id=:source AND is_demo=:demo AND published_seq>:seq
                ORDER BY published_seq LIMIT :limit
            """), {"source": target.source_id, "demo": target.is_demo,
                    "seq": row["scanned_seq"], "limit": batch_size}).mappings().all()
            if versions:
                count += enqueue_versions(conn, [v["version_id"] for v in versions], configured=[target])
                if target.limit is not None and _records(conn, target) >= target.limit:
                    continue  # 这一批里收满了：水位不动（没收的那些以后去掉上限时还要扫到）
                conn.execute(text("""
                    UPDATE index_targets SET scanned_seq=:seq,scanned_at=clock_timestamp() WHERE id=:id
                """), {"seq": versions[-1]["published_seq"], "id": target.key()})
    return {"enqueued": count, "configured_targets": len(configured)}


# ---- worker: 领一批 → 逐条发给桥接 → 记结果 ----------------------------------------------------------------

_CLAIM = """
SELECT p.*,t.config,t.enabled FROM index_publications p JOIN index_targets t ON t.id=p.target_id
WHERE p.state<>'disabled' AND p.next_check_at<=clock_timestamp() AND {which}
ORDER BY p.next_check_at,p.id LIMIT :n FOR UPDATE OF p SKIP LOCKED
"""
# 先领发出过的（查解析进度、撤下、复查），再领没发出过的（上传，新增先于回填）：查进度不会被一大批待上传的挡住；
# 在解析中的多了，一批里留给上传的位置就少，上传自然慢下来
_CLAIM_ORDER = ("p.first_send_at IS NOT NULL", "p.first_send_at IS NULL AND p.lane='new'",
                "p.first_send_at IS NULL AND p.lane<>'new'")


def _claim(batch):
    """领一批到期待办（FOR UPDATE SKIP LOCKED：别的进程正在领的跳过），领到的下次检查推后 CLAIM_SECONDS。"""
    rows = []
    with records.fabric_engine().begin() as conn:
        for which in _CLAIM_ORDER:
            if len(rows) < batch:
                rows += conn.execute(text(_CLAIM.format(which=which)), {"n": batch - len(rows)}).mappings().all()
        if rows:
            conn.execute(text(
                "UPDATE index_publications SET next_check_at=clock_timestamp()+make_interval(secs=>:s) "
                "WHERE id=ANY(:ids)"), {"s": CLAIM_SECONDS, "ids": [r["id"] for r in rows]})
    return [dict(r) for r in rows]


def work_once(*, client=None, batch=BATCH):
    """领一批到期待办，逐条处理并写回结果。返回 {结果状态: 条数}；空字典表示没有到期的。"""
    owned = client is None
    client = client or WeKnoraIndexClient()
    counts = {}
    try:
        for row in _claim(batch):
            try:
                result = _handle(row, client)
            except Exception as exc:  # noqa: BLE001 -- 记下原因，退避重试，有限次数后停
                result = _failed(row, exc)
            _save(row, result)
            counts[result["state"]] = counts.get(result["state"], 0) + 1
    finally:
        if owned:
            client.close()
    return counts


def _handle(row, client):
    """一条待办的结果（还没写库）：{state, delay[, knowledge_id, error, attempts]}。"""
    row["desired_state"] = wanted = _wanted(row)
    if wanted == "retiring":
        return {"state": row["state"], "delay": HOLD_SECONDS}  # 替代它的正文还没整份 ready：保留，不联网
    if wanted != "active":
        return _withdraw(row, client, wanted)
    if row["state"] == "ready":
        return {"state": "ready", "delay": RECHECK_SECONDS}  # 还该在，已经 ready：不联网
    if row["state"] == "blocked":  # 停住的不再上传（等管理页放回重试）；每天看一次要不要撤下
        return {"state": "blocked", "attempts": row["attempts"], "error": row["error"], "delay": RECHECK_SECONDS}
    return _publish(row, client)


def _never_sent(row):
    # 改造前的行可能有知识编号、回执而没有首次发送时间：那也算发出过
    return row["first_send_at"] is None and not row["knowledge_id"] and not row["receipt"]


def _wanted(row):
    """这条待办现在该不该在 WeKnora 里：active 该在；revoked 来源撤权或目标停用；superseded 这份正文（或这一片）
    已被替代；retiring 将被替代，但替代它的正文还没整份 ready（先上后下，不撤掉最后的入口）。"""
    target = row["config"]
    with records.fabric_engine().begin() as conn:
        if not row["enabled"] or not _readable(conn, target["source_id"]):
            return "revoked"
        versions = [v for (v,) in conn.execute(text(
            "SELECT version_id FROM " + records._VERSIONS + " WHERE source_id=:source AND is_demo=:demo "
            "AND source_record_id=:native AND " + records._ROLE + "!='context_reference'"
        ), {"source": target["source_id"], "demo": target["is_demo"], "native": row["source_record_id"]})]
        current = {artifact_key(a): a for a in current_artifacts(conn, versions).values()}
        mine = next((a for k, a in current.items() if same_text(k, row["artifact_key"])), None)
        if mine is not None and row["piece_key"] in index_pieces.keys(mine["body"], mine.get("segments") or []):
            return "active"
        # 从没发出过的，或者产物还是当前的、只是分片计划换了而这一片没 ready 过的：直接替代
        if _never_sent(row) or (mine is not None and row["state"] != "ready"):
            return "superseded"
        replacements = [mine] if mine is not None else list(current.values())
        if replacements and all(artifact_ready(conn, row["target_id"], row["source_record_id"], a)
                                for a in replacements):
            return "superseded"
        return "retiring"


def _withdraw(row, client, reason):
    """撤下一条：从没发出过的只在本地停用；发出过的请桥接逐块停用，桥接确认停完才算 disabled。"""
    if _never_sent(row):
        return {"state": "disabled", "delay": RECHECK_SECONDS}
    receipt = client.disable(row["remote_id"], reason)
    done = (receipt.get("source_status") == "disabled" and receipt.get("vector_count") == 0
            and receipt.get("index_status") == receipt.get("vector_status") == "disabled"
            and receipt.get("parse_status") in PARSE_ENDED)
    return {"state": "disabled" if done else "disabling", "delay": POLL_SECONDS}


def _publish(row, client):
    """该在的一条：先向桥接查回执（发过而没收到回执的不重复裸传），还没绑定就上传，再按回执定状态。"""
    with records.fabric_engine().begin() as conn:
        full = _artifact_text(conn, row["version_id"], row["artifact_key"]) or ""
    content = index_pieces.content(full, row["piece"]) if row["piece"] else full
    if not content.strip():
        raise FabricError("index_content_empty", "清洗产物为空，原件仍保留", 422)
    if not index_pieces.fits(content):
        return _stopped("index_content_budget", "超过单条上传预算（%d 字、%d 字节），原件与清洗产物保留，记为覆盖缺口"
                        % (len(content), len(content.encode())))
    receipt = client.status(row["remote_id"])
    if not (receipt and receipt.get("knowledge_id") and receipt.get("binding_status") == "bound"):
        _mark_sent(row["id"])
        receipt = client.publish(_body(row, content))
    return _settled(receipt)


def _settled(receipt):
    """桥接回执 → 结果。ready：上传、解析、向量、索引、绑定都完成；还在解析就过一会儿再查。"""
    if receipt.get("index_status") == "duplicate_content":  # 这个知识库里已有内容一模一样的另一条：不重复收
        return _stopped("index_duplicate_content", "知识库里已有内容完全相同的另一条记录，这一条没有重复收")
    if receipt.get("index_status") == "budget_exceeded":  # 上游分出的块超过桥接的核验预算：重试也不会好
        return _stopped("index_chunk_budget", "实际块数超过核验预算（%s 块），记为覆盖缺口" % receipt.get("chunk_count"))
    if receipt.get("source_status") != "active":
        if receipt.get("source_change_type") == "disabled_upstream" and receipt.get("parse_status") in PARSING:
            return {"state": "indexing", "delay": POLL_SECONDS}
        raise FabricError("index_upstream_disabled", "索引已停用，须明确恢复授权后再启用", 409)
    ready = (bool(receipt.get("knowledge_id")) and receipt.get("upload_status") == "uploaded"
             and receipt.get("parse_status") in {"completed", "finalizing"}
             and receipt.get("vector_status") == receipt.get("index_status") == "ready"
             and receipt.get("binding_status") == "bound")
    return {"state": "ready" if ready else "indexing", "knowledge_id": receipt.get("knowledge_id"),
            "delay": RECHECK_SECONDS if ready else POLL_SECONDS}


def _stopped(code, message):
    """重试也不会好的：直接停在 blocked，记明原因（原件与清洗产物都保留）。"""
    return {"state": "blocked", "attempts": MAX_ATTEMPTS, "error": {"code": code, "message": message},
            "delay": RECHECK_SECONDS}


def _failed(row, exc):
    """出错：记下原因，退避重试（20 秒起翻倍，最多 1 小时），连续 MAX_ATTEMPTS 次后停在 blocked。"""
    attempts = row["attempts"] + 1
    if isinstance(exc, FabricError):
        error = {"code": exc.code, "message": exc.message}
    else:
        error = {"code": "index_unavailable", "message": "索引处理暂不可用，稍后重试"}
    return {"state": "blocked" if attempts >= MAX_ATTEMPTS else "waiting", "attempts": attempts, "error": error,
            "delay": min(3600, 10 * 2 ** attempts)}


def _mark_sent(job_id):
    """上传之前先记下“发出过”：万一请求发出后进程退出，下次先向桥接对账，不当作没发过。"""
    with records.fabric_engine().begin() as conn:
        conn.execute(text(
            "UPDATE index_publications SET first_send_at=COALESCE(first_send_at,clock_timestamp()),state='sending',"
            "updated_at=clock_timestamp() WHERE id=:id"), {"id": job_id})


def _save(row, result):
    """写回一条的结果；刚变 ready 时，同一记录里等着交接的其他正文的条目马上复查。"""
    with records.fabric_engine().begin() as conn:
        conn.execute(text("""
            UPDATE index_publications SET state=:state,desired_state=:desired,error=CAST(:error AS jsonb),
              attempts=:attempts,knowledge_id=COALESCE(:knowledge,knowledge_id),
              ready_at=CASE WHEN :state='ready' THEN COALESCE(ready_at,clock_timestamp()) ELSE ready_at END,
              next_check_at=clock_timestamp()+make_interval(secs=>:delay),updated_at=clock_timestamp()
            WHERE id=:id
        """), {"id": row["id"], "state": result["state"], "desired": row["desired_state"],
               "error": canonical(result["error"]) if result.get("error") else None,
               "attempts": result.get("attempts", 0), "knowledge": result.get("knowledge_id"),
               "delay": result["delay"]})
        if result["state"] == "ready" and row["state"] != "ready":
            conn.execute(text(
                "UPDATE index_publications SET next_check_at=clock_timestamp() WHERE target_id=:t "
                "AND source_record_id=:n AND artifact_key<>:k AND state<>'disabled'"
            ), {"t": row["target_id"], "n": row["source_record_id"], "k": row["artifact_key"]})


def configured():
    """有索引目标配置，或库里还有目标（配置移除＝停止使用，旧条目仍要撤下）。"""
    if os.environ.get("DATAFABRIC_INDEX_TARGETS"):
        return True
    if not os.environ.get("DATAFABRIC_DATABASE_URL"):
        return False
    with records.fabric_engine().begin() as conn:
        if conn.execute(text("SELECT to_regclass('index_targets')")).scalar_one() is None:
            return False
        return conn.execute(text("SELECT EXISTS(SELECT 1 FROM index_targets)")).scalar_one()


def run_worker(*, idle_seconds=5.0, scan_every_seconds=30.0, stop=None, rounds=None):
    """常驻 worker：一轮先按间隔补扫（scan_committed），再领一批待办处理（work_once）；没活、或库暂不可用，就睡
    idle_seconds。stop（threading.Event）置位或跑满 rounds 轮后返回各结果的计数。"""
    targets()  # 配置错误（例如配了已退役的来源）在这里就停，不等到第一次检索
    stop = stop or threading.Event()
    client = WeKnoraIndexClient()
    counts, last_scan, n = {}, None, 0
    try:
        while not stop.is_set() and (rounds is None or n < rounds):
            n += 1
            try:
                if last_scan is None or time.monotonic() - last_scan >= scan_every_seconds:
                    last_scan = time.monotonic()
                    scan_committed()
                done = work_once(client=client)
            except Exception:  # noqa: BLE001 -- 库或配置暂不可用：待办和水位都在库里，稍后再来
                done = {"unavailable": 1}
            for state, k in done.items():
                counts[state] = counts.get(state, 0) + k
            if not done or "unavailable" in done:
                stop.wait(idle_seconds)
    finally:
        client.close()
    return counts


# ---- 管理页（graph/frontend 的索引发布页）：列表、计数、重试 -----------------------------------------------

# index_publications.state 的全部取值（流水线总览和管理页按这个顺序显示）
STATES = ("queued", "sending", "indexing", "ready", "waiting", "blocked", "disabling", "disabled")
_JOB_ID = re.compile(r"[0-9a-f]{64}")


def _job_id(key):
    if not isinstance(key, str) or not _JOB_ID.fullmatch(key):
        raise FabricError("index_publication_id", "索引作业编号须为64位小写十六进制", 422)
    return key


def _cursor(value):
    """续页标记是上一页最后一条的“更新时间|作业编号”；格式不对就拒绝，不猜。"""
    try:
        stamp, key = value.rsplit("|", 1)
        moment = parse_timestamp(stamp)
        if moment.tzinfo is None:
            raise ValueError()
        return moment, _job_id(key)
    except (ValueError, AttributeError, FabricError):
        raise FabricError("index_cursor", "索引列表续页标记无效", 422) from None


_LISTED = ("id", "target_id", "version_id", "source_record_id", "remote_id", "state", "desired_state", "knowledge_id",
           "receipt", "error", "attempts", "first_send_at", "next_check_at", "updated_at", "artifact_key", "lane",
           "piece_index", "piece", "created_at", "ready_at")


def status(*, limit=100, state=None, source_id=None, cursor=None):
    """索引作业列表：按更新时间倒序，可按状态、来源过滤；next_cursor 不为空时还有下一页。"""
    if not 1 <= limit <= 100:
        raise FabricError("index_budget", "索引状态最多读取100项")
    if state is not None and state not in STATES:
        raise FabricError("index_filter", "索引状态过滤值无效", 422)
    if source_id is not None and not 1 <= len(source_id) <= 80:
        raise FabricError("index_filter", "来源编号过滤值无效", 422)
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
    with records.fabric_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT p.*,t.config FROM index_publications p JOIN index_targets t ON t.id=p.target_id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY p.updated_at DESC,p.id DESC LIMIT :limit"
        ), params).mappings().all()
        items = [{k: row.get(k) for k in _LISTED} | {"title": (row["manifest"] or {}).get("title"),
                                                     "source": row["config"],
                                                     "read_permitted": _readable(conn, row["config"]["source_id"])}
                 for row in rows[:limit]]
    last = rows[limit - 1] if len(rows) > limit else None
    return {"items": items, "next_cursor": last["updated_at"].isoformat() + "|" + last["id"] if last else None}


def state_counts():
    """各索引发布状态的条数（流水线总览用）。"""
    with records.fabric_engine().begin() as conn:
        return {r[0]: r[1] for r in conn.execute(text(
            "SELECT state, count(*) FROM index_publications GROUP BY state"))}


def retry(key):
    """把停住（blocked）的作业放回待重试：尝试次数清零、立即到期，由索引 worker 处理（处理时仍先向桥接查回执，
    不会二次裸上传）。其余状态不动，如实返回当前状态。"""
    key = _job_id(key)
    with records.fabric_engine().begin() as conn:
        if conn.execute(text(
            "UPDATE index_publications SET state='waiting',attempts=0,next_check_at=clock_timestamp(),"
            "updated_at=clock_timestamp() WHERE id=:id AND state='blocked'"
        ), {"id": key}).rowcount == 1:
            return {"id": key, "scheduled": True, "state": "waiting"}
        found = conn.execute(text("SELECT state FROM index_publications WHERE id=:id"), {"id": key}).mappings().first()
    if found is None:
        raise FabricError("index_publication_not_found", "没有这条索引作业", 404)
    return {"id": key, "scheduled": False, "state": found["state"], "reason": "not_blocked"}
