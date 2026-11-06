"""抽取/审核的主体。

抽取、审核、消歧只有一套，以 documents 行为主体。主体有两种来源：
- documents 库存里的文档：正文在 raw_content；
- history 的一个固定版本（新采集的材料只在 history 里）：行里只存 (来源, 记录, 版本) 和所用清洗正文的
  (处理器版本, sha256)，不复制正文。抽取时按引用读那一份清洗正文，并核对哈希——v1 排队后 v2 到来，
  v1 的任务仍读 v1；抽出的候选挂在 v1 的主体上，回指 v1 的原件和清洗产物。

入队时可见不等于永久授权：每次读正文（抽取任务、详情页）都先过 history 的权威访问检查（read_version：
来源许可与开关、版本可见性、演示隔离），撤权后不再读出、不再送去抽取；列表里也不出现撤权来源的主体。
一个版本只有一个主体、一份绑定：重复登记返回已绑定的产物，不暗换；清洗器升级后的新产物在回执里标出，
要用新产物重新抽取是另一项显式操作（目前未提供）。
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import NamedTuple

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
            doc = Document(
                uuid=key, title=(record.title or "")[:2000], source_url=record.source_url or "",
                source_site=source_id, raw_content="", doc_type=(got.get("profile") or {}).get("doc_type") or "",
                language=clean.get("language") or "", parse_method="history_version",
                extraction_status="queued", extraction_error="",
                history_source_id=source_id, history_record_id=record_id, history_version_id=version_id,
                history_text_version=clean["processor_version"], history_text_sha256=clean["text_sha256"],
                history_is_demo=is_demo,
            )
            session.add(doc)
            created = True
        elif doc.extraction_status not in ("queued", "processing"):
            doc.extraction_status, doc.extraction_error = "queued", ""
        pinned = (doc.history_text_version, doc.history_text_sha256)
    from flows.extraction_flow import run_extraction_for_uuids

    run_extraction_for_uuids([key])
    return {"uuid": key, "created": created, "source_id": source_id, "record_id": record_id,
            "version_id": version_id, "text_processor_version": pinned[0], "text_sha256": pinned[1],
            "newer_text": None if pinned == (clean["processor_version"], clean["text_sha256"]) else {
                "text_processor_version": clean["processor_version"], "text_sha256": clean["text_sha256"]}}


def check_access(doc) -> None:
    """history 主体的权威访问检查（与 history 读取同一个 read_version）：不可见就抛 HistoryError。"""
    from db.history import read_version

    read_version(doc.history_version_id, doc.history_source_id, doc.history_record_id,
                 bool(doc.history_is_demo), "current", datetime.now(timezone.utc))


def readable_sources() -> set[str]:
    """当前可读的 history 来源（列表过滤用）；history 库不可用时返回空集——宁可不列，不列出撤权的。"""
    from db import history

    try:
        return {row["source_id"] for row in history.list_sources()}
    except Exception:  # noqa: BLE001
        return set()


def listed(document_model):
    """列表查询的过滤条件：documents 库存照常；history 主体只列当前可读来源的。"""
    from sqlalchemy import or_

    return or_(document_model.history_version_id.is_(None),
               document_model.history_source_id.in_(sorted(readable_sources())))


def subject_text(doc) -> str:
    """主体的正文：documents 库存直接用 raw_content；history 固定版本先过访问检查，再按引用读清洗正文并核对哈希。"""
    if not getattr(doc, "history_version_id", None):
        return doc.raw_content or ""
    check_access(doc)
    row = _clean_text(doc.history_version_id, doc.history_text_version)
    if row is None:
        raise HistoryError("subject_text_missing", "主体引用的清洗正文不存在", 409)
    if row["text_sha256"] != doc.history_text_sha256:
        raise HistoryError("subject_text_changed", "主体引用的清洗正文与登记时的哈希不一致", 409)
    return row["text"] or ""


class Subject(NamedTuple):
    text: str
    source_site: str
    doc_type: str
    # documents 库存的正文是来源原件（可按原字段做规则映射）；history 主体的正文是清洗正文
    is_history: bool


def load_subject(uuid: str) -> Subject | None:
    """主体的正文、来源站点、文档类型；主体不存在返回 None。抽取 worker 和 RQ 任务共用这一个读取。"""
    from db.models import Document
    from db.session import get_session

    with get_session() as session:
        if session is None:
            raise HistoryError("db_unavailable", "抽取库未连接", 503)
        doc = session.query(Document).filter(Document.uuid == uuid, Document.is_deleted == False).first()  # noqa: E712
        if doc is None:
            return None
        return Subject(subject_text(doc), doc.source_site or "", doc.doc_type or "",
                       bool(getattr(doc, "history_version_id", None)))
