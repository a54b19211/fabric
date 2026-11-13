"""站点以外来源的附件版本（PubMed 的 PMC 全文 / OA PDF、本地导入的原件）：与站点附件同一契约。

站点插件的附件版本（fetch.site_crawler）是唯一的附件形状：schema datafabric.attachment.v1、role=attachment、
原件按 sha256 落盘（fetch.web_client.store_original）、正文由加工层 clean.attachment_parse
按版本产出（clean.derived_text，来源集合 fetch.catalog.ATTACHMENT_SOURCES）。这里只补两件事：
- 许可与可取得层级（access）：为什么可以保存这份原件，写进原件 JSON，投影进 facts.access；
- 本地导入没有父页面：记录身份是 local-file:<集合>/<文件名>，不挂父记录（不写“父版本未知”的假关系）。
有父记录的（PubMed）身份仍是“父记录#attachment=链接”，投影直接用站点插件的 project_capture。
"""

from __future__ import annotations

import hashlib
import json
import re

from store.history_models import ArchiveRecord, HistoryError

ATT_SCHEMA = "datafabric.attachment.v1"
PARSER_VERSION = "datafabric.attachment.access.v1"
LOCAL_PREFIX = "local-file:"


def local_record_id(collection: str, name: str) -> str:
    """本地原件的记录身份：集合 + 集合内文件名。同名新内容是同一记录的新版本；版本按 sha256 固定。"""
    rid = LOCAL_PREFIX + collection + "/" + name
    if len(rid) > 400:
        rid = LOCAL_PREFIX + collection + "/sha256-" + hashlib.sha256(name.encode()).hexdigest()
    return rid


def attachment_record(
    source_id: str,
    *,
    record_id: str,
    stored: dict,
    file_name: str,
    file_ext: str,
    discovered_from: str,
    access: dict,
    url: str | None = None,
    parent_record_id: str | None = None,
    extra: dict | None = None,
) -> ArchiveRecord:
    """一条附件版本的原件（JSON 元数据；文件本身在 stored["local_path"]）。stored 来自 store_original 或
    download_pdf（含 local_path / sha256 / size）。"""
    data = {
        "schema_version": ATT_SCHEMA,
        "role": "attachment",
        "site": source_id,
        "url": url,
        "parent_record_id": parent_record_id,
        "file_name": file_name,
        "file_ext": file_ext.lower(),
        "sha256": stored["sha256"],
        "size": stored["size"],
        "store_path": stored["local_path"],
        "discovered_from": discovered_from,
        "status": "stored",
        "access": access,
        **(extra or {}),
    }
    return ArchiveRecord(
        source_record_id=record_id,
        record_kind="document",
        raw_content=json.dumps(data, ensure_ascii=False, sort_keys=True),
    )


def project_attachment(source_id: str, item: ArchiveRecord) -> dict:
    """站点以外来源的附件原件 → 确定性投影（facts 键与站点附件相同，另加 access / collection）。"""
    try:
        data = json.loads(item.raw_content)
    except ValueError:
        raise HistoryError("invalid_source_fields", "附件原件不是合法 JSON") from None
    if not isinstance(data, dict) or data.get("role") != "attachment" or data.get("schema_version") != ATT_SCHEMA:
        raise HistoryError("invalid_source_fields", "原件不符合 attachment 契约")
    access = data.get("access")
    if not isinstance(access, dict) or not access.get("tier"):
        raise HistoryError("invalid_source_fields", "附件原件缺少许可与可取得层级（access）")
    if data.get("parent_record_id"):
        from fetch.site_crawler import project_capture

        projected = project_capture(source_id, item)  # 身份、来源、字段与站点附件同一规则
        projected["facts"]["access"] = access
        projected["parser_version"] = PARSER_VERSION
        return projected
    collection, name = data.get("collection"), data.get("collection_name")
    if not isinstance(collection, str) or not isinstance(name, str) or data.get("site") != source_id:
        raise HistoryError("invalid_source_fields", "本地原件缺少集合或文件名")
    if local_record_id(collection, name) != item.source_record_id:
        raise HistoryError("native_id_mismatch", "本地原件的集合/文件名与记录不一致")
    if not re.fullmatch(r"[0-9a-f]{64}", str(data.get("sha256") or "")) or not isinstance(data.get("size"), int):
        raise HistoryError("invalid_source_fields", "本地原件缺少 sha256 或大小")
    facts = {
        k: data.get(k)
        for k in ("parent_record_id", "file_name", "file_ext", "sha256", "size", "store_path", "discovered_from")
    }
    facts.update(role="attachment", access=access, collection=collection, collection_name=name)
    return {
        "record_kind": "document",
        "title": data.get("file_name") or name,
        "source_url": None,
        "clean_text": "",
        "facts": facts,
        "field_locators": {},
        "event_time": None,
        "source_published_at": None,
        "source_updated_at": None,
        "parser_version": PARSER_VERSION,
    }
