"""原件提交后的持久索引待办；只依赖已有History库和发表循环。"""

import json
import os
from uuid import uuid4

from pydantic import Field
from sqlalchemy import text

from biz_rules.history_models import HistoryError, HistoryRecord, Model, canonical, sha256
from core_engine.weknora_index_client import WeKnoraIndexClient, publication_id
from db import history


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


def _manifest(target, record):
    if target["source_id"] != record.source_id or target["is_demo"] is not record.is_demo:
        raise HistoryError("index_source_space_mismatch", "索引目标与固定原件来源空间不一致", 409)
    identity = {
        "source_instance": target["source_instance"], "source_id": record.source_id,
        "source_record_id": record.source_record_id, "version_id": record.version_id,
        "raw_sha256": record.raw_sha256,
    }
    return {
        **identity, "is_demo": record.is_demo, "knowledge_base_id": target["knowledge_base_id"],
        "source_scope": target["source_scope"],
        "raw_locator": {k: identity[k] for k in identity if k != "raw_sha256"},
        "title": (record.title or record.source_record_id)[:255],
        "content_sha256": sha256(record.clean_text),
    }


def scan_committed(*, batch_size=25):
    """落任务和推进扫描位置在同一事务；已提交原件是唯一输入。"""
    if not 1 <= batch_size <= 100:
        raise HistoryError("index_budget", "索引扫描批次须为1至100")
    configured = targets()
    engine = history.history_engine()
    count = 0
    with engine.begin() as conn:
        for target in configured:
            conn.execute(text("""
                INSERT INTO index_targets(id,config) VALUES (:id,CAST(:config AS jsonb))
                ON CONFLICT(id) DO UPDATE SET enabled=true
            """), {"id": target.key(), "config": target.model_dump_json()})
        conn.execute(
            text("UPDATE index_targets SET enabled=false WHERE NOT(id=ANY(:ids))"),
            {"ids": [item.key() for item in configured]},
        )
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
                SELECT payload,published_seq FROM history_versions
                WHERE source_id=:source AND is_demo=:demo AND published_seq>:seq
                ORDER BY published_seq LIMIT :limit
            """), {"source": target.source_id, "demo": target.is_demo,
                    "seq": row["scanned_seq"], "limit": batch_size}).mappings().all()
            for version in versions:
                record = HistoryRecord.model_validate(version["payload"])
                if record.record_kind == "graph":
                    continue
                manifest = _manifest(target.model_dump(), record)
                key = sha256(canonical([target.key(), record.version_id]))
                conn.execute(text("""
                    INSERT INTO index_publications
                      (id,target_id,version_id,source_record_id,remote_id,manifest)
                    VALUES (:id,:target,:version,:native,:remote,CAST(:manifest AS jsonb))
                    ON CONFLICT(target_id,version_id) DO NOTHING
                """), {"id": key, "target": target.key(), "version": record.version_id,
                        "native": record.source_record_id, "remote": publication_id(manifest),
                        "manifest": canonical(manifest)})
                count += 1
            if versions:
                conn.execute(text("""
                    UPDATE index_targets SET scanned_seq=:seq,scanned_at=clock_timestamp() WHERE id=:id
                """), {"seq": versions[-1]["published_seq"], "id": target.key()})
    return {"scanned_records": count, "configured_targets": len(configured)}


def _claim():
    with history.history_engine().begin() as conn:
        row = conn.execute(text("""
            SELECT p.*,t.config,t.enabled FROM index_publications p
            JOIN index_targets t ON t.id=p.target_id
            WHERE p.next_check_at<=clock_timestamp()
              AND (p.lease_until IS NULL OR p.lease_until<clock_timestamp())
            ORDER BY p.next_check_at,p.id FOR UPDATE OF p SKIP LOCKED LIMIT 1
        """)).mappings().first()
        if row is None:
            return None
        token = uuid4().hex
        conn.execute(text("""
            UPDATE index_publications SET lease_token=:token,
              lease_until=clock_timestamp()+interval '120 seconds',
              updated_at=clock_timestamp() WHERE id=:id
        """), {"id": row["id"], "token": token})
        return dict(row, lease_token=token)


def _current_state(row):
    """查当前权限与原件头版；采用序号不等于来源事件的新旧。"""
    target = row["config"]
    with history.history_engine().begin() as conn:
        enabled = conn.execute(
            text("SELECT enabled FROM index_targets WHERE id=:id"), {"id": row["target_id"]}
        ).scalar_one()
        if not enabled:
            return "revoked", None
        try:
            history._source(conn, target["source_id"])
        except HistoryError as exc:
            if exc.status == 403:
                return "revoked", None
            raise
        current = conn.execute(text(
            "SELECT version_id,payload FROM history_versions "
            "WHERE source_id=:source AND is_demo=:demo AND source_record_id=:native "
            "AND COALESCE(payload->'facts'->>'publication_role','source_record')!='context_reference' "
            "ORDER BY COALESCE(proof_available_at,first_seen_at) DESC,"
            + history._SOURCE_UPDATED_ORDER
            + " DESC NULLS LAST,published_seq DESC LIMIT 1"
        ), {"source": target["source_id"], "demo": target["is_demo"],
            "native": row["source_record_id"]}).mappings().first()
        if current is None or current["version_id"] != row["version_id"]:
            return "superseded", None
        record = HistoryRecord.model_validate(current["payload"])
        if record.facts.get("publication_state") in {"withdrawn", "deleted", "retracted"}:
            return "withdrawn", None
        return "active", record


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
        receipt.get("is_demo", False) is not row.get("config", {}).get("is_demo", expected.get("is_demo", False))
        or receipt.get("publication_id") != row["remote_id"]
        or receipt.get("knowledge_base_id") != expected["knowledge_base_id"]
        or receipt.get("source_scope") != expected["source_scope"]
        or receipt.get("content_sha256") != expected["content_sha256"]
        or any((receipt.get("identity") or {}).get(k) != expected[k]
               for k in ["source_instance", "source_id", "source_record_id", "version_id", "raw_sha256"])
        or receipt.get("raw_locator") != expected["raw_locator"]
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
              next_check_at=clock_timestamp()+make_interval(secs=>:delay),updated_at=clock_timestamp()
            WHERE id=:id AND lease_token=:token AND lease_until>clock_timestamp()
        """), {"id": row["id"], "token": row["lease_token"], "state": state,
                "desired": desired, "receipt": canonical(receipt) if receipt is not None else None,
                "knowledge": receipt.get("knowledge_id") if receipt else None,
                "error": canonical(error) if error else None, "attempts": attempts, "delay": delay})
        if result.rowcount != 1:
            raise HistoryError("index_lease_lost", "迟到索引回执未覆盖当前处理资格", 409)
    return {"id": row["id"], "state": state, "desired_state": desired}



def _hold_blocked(row):
    # 不无限重发失败请求；保留错误，但继续检查权限/头版变化是否需要清理。
    with history.history_engine().begin() as conn:
        result = conn.execute(text("""
            UPDATE index_publications SET lease_token=NULL,lease_until=NULL,
              next_check_at=clock_timestamp()+interval '60 seconds'
            WHERE id=:id AND lease_token=:token AND lease_until>clock_timestamp()
        """), {"id": row["id"], "token": row["lease_token"]})
        if result.rowcount != 1:
            raise HistoryError("index_lease_lost", "索引处理资格已改变", 409)
    return {"id": row["id"], "state": "blocked", "desired_state": row["desired_state"]}

def process_once(*, client=None):
    row = _claim()
    if row is None:
        return {"state": "idle"}
    desired = "unknown"
    owned = client is None
    verified_receipt = None
    try:
        desired, record = _current_state(row)
        demo = row["config"]["is_demo"]
        if "is_demo" in row["manifest"] and row["manifest"]["is_demo"] is not demo:
            raise HistoryError("index_source_space_mismatch", "索引意图与目标空间不一致", 409)
        # 兼容旧意图时只从原目标配置补标记，绝不把demo降为false。
        row["manifest"] = {**row["manifest"], "is_demo": demo}
        if row["state"] == "blocked":
            if desired == row["desired_state"] and desired == "active":
                return _hold_blocked(row)
            if desired != row["desired_state"]:
                row["attempts"] = 0
        client = client or WeKnoraIndexClient()
        receipt = client.status(row["remote_id"])
        if receipt is not None:
            _validate_receipt(row, receipt)
            verified_receipt = receipt
        if desired != "active":
            if receipt is None:
                if row["first_send_at"] is not None:
                    raise HistoryError("index_disable_unknown", "曾发送的索引结果未知，尚不能确认停用", 409)
                return _finish(row, "disabled", desired, delay=300)
            _validate_receipt(row, receipt)
            verified_receipt = receipt
            receipt = client.disable(row["remote_id"], desired)
            _validate_receipt(row, receipt)
            confirmed = (
                receipt.get("source_status") == "disabled"
                and receipt.get("index_status") == receipt.get("vector_status") == "disabled"
                and receipt.get("parse_status") in {"completed", "failed", "cancelled"}
                and receipt.get("vector_count") == 0
            )
            return _finish(row, "disabled" if confirmed else "disabling", desired, receipt=receipt, delay=300 if confirmed else 15)
        if not record.clean_text.strip() or len(record.clean_text.encode()) > 2000000:
            raise HistoryError("index_content_budget", "正文为空或超过索引单条预算，原件仍保留", 422)
        if sha256(record.clean_text) != row["manifest"]["content_sha256"]:
            raise HistoryError("index_content_changed", "固定原件的清洗正文与冻结摘要不符", 409)
        if receipt is None or not receipt.get("knowledge_id") or receipt.get("binding_status") != "bound":
            _sent(row)
            # GET先核对。服务端以同一固定publication_id保留意图并恢复，不能二次裸上传。
            receipt = client.publish({**row["manifest"], "title": row["manifest"]["title"][:255],
                                      "content": record.clean_text})
        _validate_receipt(row, receipt)
        verified_receipt = receipt
        # 网络请求期间可能撤权/换头版；不能以旧资格确认ready。
        latest, _ = _current_state(row)
        if latest != "active":
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


def index_tick(*, batch_size=5):
    """接在既有发表循环；没有配置时不建任务、不联网。"""
    if not 1 <= batch_size <= 100:
        raise HistoryError("index_budget", "索引处理批次须为1至100")
    if not os.environ.get("DATAFABRIC_INDEX_TARGETS"):
        if not os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL"):
            return {"state": "disabled"}
        with history.history_engine().begin() as conn:
            exists = conn.execute(text("SELECT to_regclass('index_targets')")).scalar_one()
            if exists is None:
                return {"state": "disabled"}
            if not conn.execute(text("SELECT EXISTS(SELECT 1 FROM index_targets)")).scalar_one():
                return {"state": "disabled"}
        # 配置移除意味着停止使用；旧任务/绑定仍需撤回对账。
    scanned = scan_committed(batch_size=25)
    states = []
    for _ in range(batch_size):
        result = process_once()
        states.append(result["state"])
        if result["state"] == "idle":
            break
    return {"state": "running", **scanned, "processed": states}


def status(*, limit=100):
    if not 1 <= limit <= 100:
        raise HistoryError("index_budget", "索引状态最多读取100项")
    with history.history_engine().begin() as conn:
        rows = conn.execute(text("""
            SELECT p.*,t.config FROM index_publications p JOIN index_targets t ON t.id=p.target_id
            ORDER BY p.updated_at DESC,p.id LIMIT :limit
        """), {"limit": limit}).mappings().all()
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
                "knowledge_id","receipt","error","attempts","next_check_at","updated_at",
            ]} | {"source": row["config"], "read_permitted": permitted})
        return result


def retry(key):
    with history.history_engine().begin() as conn:
        result = conn.execute(text("""
            UPDATE index_publications SET state='waiting',attempts=0,next_check_at=clock_timestamp()
            WHERE id=:id AND state='blocked' AND (lease_until IS NULL OR lease_until<clock_timestamp())
        """), {"id": key})
        return {"id": key, "scheduled": result.rowcount == 1}
