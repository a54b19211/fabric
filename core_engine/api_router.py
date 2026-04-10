"""
API 数据路由器。
将 API 原始记录转换为系统标准解析结果结构，
与 route_webpage / route_pdf 的输出格式保持一致。

字段映射规则（field_map）：
  - 普通 key：用点号路径从原始记录取值，如 "openfda.brand_name.0"
  - "__const__:值"：固定常量，如 "__const__:药品召回"
"""

import json
import uuid as _uuid
from datetime import datetime, timezone
from typing import Iterator

from loguru import logger

from core_engine.api_client import iter_api_records


def _make_uuid(identifier: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, identifier))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_nested(record: dict, dotted_key: str) -> str:
    """支持点号路径取值，如 "openfda.brand_name.0"，取不到返回空字符串。"""
    if not dotted_key:
        return ""
    if dotted_key.startswith("__const__:"):
        return dotted_key.split(":", 1)[1]

    val = record
    for part in dotted_key.split("."):
        if isinstance(val, dict):
            val = val.get(part, "")
        elif isinstance(val, list):
            try:
                val = val[int(part)]
            except (IndexError, ValueError):
                return ""
        else:
            return ""
    return str(val) if val else ""


def _record_to_markdown(record: dict, title: str) -> str:
    """把原始记录转成简单 Markdown，方便质检和存储。"""
    lines = [f"# {title}" if title else "# (无标题)", ""]
    for k, v in record.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        lines.append(f"**{k}**: {v}")
    return "\n".join(lines)


def iter_api_results(site_key: str, profile: dict) -> Iterator[dict]:
    """
    根据站点配置拉取 API 数据，逐条 yield 标准解析结果 dict。
    输出结构与 route_webpage / route_pdf 保持一致。
    """
    field_map = profile.get("field_map", {})
    uid_field = profile.get("uid_field", "")  # 站点指定用哪个字段做 UUID 种子

    for record in iter_api_records(profile):
        # fetcher 已经返回标准格式（含 uuid/markdown/structured_data）时直接透传，跳过字段映射
        if "uuid" in record and "markdown" in record and "structured_data" in record:
            if record.get("source_site") or record.get("parse_method"):
                yield record
                continue

        # 生成幂等 UUID
        uid_src = (
            _get_nested(record, uid_field) if uid_field
            else json.dumps(record, sort_keys=True)
        )
        doc_uuid = _make_uuid(f"{site_key}:{uid_src}")

        title = _get_nested(record, field_map.get("title", ""))
        publish_date = _get_nested(record, field_map.get("publish_date", ""))
        summary = _get_nested(record, field_map.get("summary", ""))
        drug_name = _get_nested(record, field_map.get("drug_name", ""))
        manufacturer = _get_nested(record, field_map.get("manufacturer", ""))
        category = _get_nested(record, field_map.get("category", ""))

        markdown = _record_to_markdown(record, title)

        # source_url：优先用 field_map 指定的字段，其次 record 自带，最后用配置的 base_url
        source_url_field = field_map.get("source_url", "")
        source_url = (
            _get_nested(record, source_url_field)
            or profile.get("api", {}).get("base_url", "")
        )

        # sniffed_meta：由 field_map 中 "sniffed_meta.*" 前缀的键组装，供置信度评估使用
        sniffed_meta = {
            k.split(".", 1)[1]: _get_nested(record, v)
            for k, v in field_map.items()
            if k.startswith("sniffed_meta.")
        }

        yield {
            "uuid": doc_uuid,
            "title": title,
            "publish_date": publish_date,
            "markdown": markdown,
            "structured_data": {
                "title": title,
                "publish_date": publish_date,
                "category": category,
                "summary": summary[:500] if summary else "",
                "drug_name": drug_name,
                "manufacturer": manufacturer,
            },
            "parse_method": "api",
            "attachment_links": [],
            "source_url": source_url,
            "source_site": site_key,
            "sniffed_meta": sniffed_meta,
            "timestamp": _now_iso(),
        }
