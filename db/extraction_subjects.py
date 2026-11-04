"""抽取/审核的主体。

抽取、审核、消歧只有一套，以 documents 行为主体。主体有两种来源：
- documents 库存里的文档：正文在 raw_content；
- history 的一个固定版本（新采集的材料只在 history 里）：行里只存 (来源, 记录, 版本) 和所用清洗正文的
  (处理器版本, sha256)，不复制正文。抽取时按引用读那一份清洗正文，并核对哈希——v1 排队后 v2 到来，
  v1 的任务仍读 v1；抽出的候选挂在 v1 的主体上，回指 v1 的原件和清洗产物。
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

from sqlalchemy import text

from biz_rules.history_models import HistoryError


def subject_uuid(source_id: str, record_id: str, version_id: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"history:{source_id}:{record_id}:{version_id}"))


def _clean_text(version_id: str, processor_version: str) -> dict | None:
    from db.history import history_engine

    with history_engine().begin() as conn:
        row = conn.execute(text(
            "SELECT text, text_sha256, status FROM derived_clean_text WHERE version_id=:v AND processor_version=:pv "
            "ORDER BY attempt DESC LIMIT 1"), {"v": version_id, "pv": processor_version}).mappings().first()
    return dict(row) if row else None


def enqueue_version(source_id: str, record_id: str, version_id: str, *, is_demo: bool = False) -> dict:
    """把一个固定版本登记为抽取主体（幂等）并排进抽取队列。版本须当前可见且已有清洗正文。"""
    from db import clean_stage
    from db.models import Document
    from db.session import get_session

    now = datetime.now(timezone.utc)
    got = clean_stage.for_versions(source_id, is_demo, [version_id], "current", now)["versions"][version_id]
    if got.get("state") != "processed":
        raise HistoryError("subject_not_cleaned", "该版本当前不可见或还没有清洗正文，不能抽取", 409)
    clean = got["clean"]
    if got.get("source_record_id") != record_id:
        raise HistoryError("native_id_mismatch", "版本不属于这条记录")
    if clean.get("status") != "parsed" or not (clean.get("text") or "").strip():
        raise HistoryError("subject_no_text", "该版本没有可用清洗正文（解析失败或空）", 409)
    from db.history import read_version

    record = read_version(version_id, source_id, record_id, is_demo, "current", now)
    key = subject_uuid(source_id, record_id, version_id)
    created = False
    with get_session() as session:
        if session is None:
            raise HistoryError("db_unavailable", "抽取库未连接", 503)
        doc = session.query(Document).filter(Document.uuid == key).first()
        if doc is None:
            session.add(Document(
                uuid=key, title=(record.title or "")[:2000], source_url=record.source_url or "",
                source_site=source_id, raw_content="", doc_type=(got.get("profile") or {}).get("doc_type") or "",
                language=clean.get("language") or "", parse_method="history_version",
                extraction_status="queued", extraction_error="",
                history_source_id=source_id, history_record_id=record_id, history_version_id=version_id,
                history_text_version=clean["processor_version"], history_text_sha256=clean["text_sha256"],
                history_is_demo=is_demo,
            ))
            created = True
        elif doc.extraction_status not in ("queued", "processing"):
            doc.extraction_status, doc.extraction_error = "queued", ""
    from flows.extraction_flow import run_extraction_for_uuids

    run_extraction_for_uuids([key])
    return {"uuid": key, "created": created, "source_id": source_id, "record_id": record_id,
            "version_id": version_id, "text_processor_version": clean["processor_version"],
            "text_sha256": clean["text_sha256"]}


def subject_text(doc) -> str:
    """主体的正文：documents 库存直接用 raw_content；history 固定版本按引用读清洗正文并核对哈希。"""
    if not getattr(doc, "history_version_id", None):
        return doc.raw_content or ""
    row = _clean_text(doc.history_version_id, doc.history_text_version)
    if row is None:
        raise HistoryError("subject_text_missing", "主体引用的清洗正文不存在", 409)
    if row["text_sha256"] != doc.history_text_sha256:
        raise HistoryError("subject_text_changed", "主体引用的清洗正文与登记时的哈希不一致", 409)
    return row["text"] or ""


def load_subject(uuid: str):
    """(正文, 来源站点, 文档类型)；主体不存在返回 None。抽取 worker 和 RQ 任务共用这一个读取。"""
    from db.models import Document
    from db.session import get_session

    with get_session() as session:
        if session is None:
            raise HistoryError("db_unavailable", "抽取库未连接", 503)
        doc = session.query(Document).filter(Document.uuid == uuid, Document.is_deleted == False).first()  # noqa: E712
        if doc is None:
            return None
        return subject_text(doc), doc.source_site or "", doc.doc_type or ""
