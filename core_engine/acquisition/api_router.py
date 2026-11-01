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

from core_engine.acquisition.api_client import iter_api_records
from biz_rules.cleaners.field.date_format import normalize_date
from core_engine.lineage.lineage_tracer import snapshot, record_step


def _make_uuid(identifier: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, identifier))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_parse_lineage(parsed_result: dict, op: str = "") -> None:
    """在 parse 步骤结束时记录关键字段的血缘（输入为空，输出为解析结果）。"""
    empty_before = {k: "" for k in snapshot(parsed_result)}
    record_step(parsed_result, step="parse", before=empty_before, op=op)


def _get_nested(record: dict, dotted_key: str) -> str:
    """字符串展示映射：保留0/False，缺字段、None及空字符串仍展示为空。

    不用于保存原始字段类型；不修改传入record，也不为缺字段补造null。
    """
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
    return str(val) if val is not None else ""


def _extract_extra_value(record: dict, spec) -> object:
    """按 extra_fields 的规格从原始记录取值。

    支持三种写法：
      - "a.b.c"：字符串展示；0/False保留，缺字段、None和空字符串仍返回 ""
      - {"path": "a.b.c", "type": "list"}：路径指向列表，返回 List[str]
      - {"path": "a.b.c", "type": "list", "key": "name"}：路径指向 dict 列表，取每项的 key 字段
      - {"path": "a.b.c", "type": "int"}：解析为整数，未知/非法返回 None（真实0保留）
    """
    if isinstance(spec, dict):
        path = spec.get("path", "")
        vtype = spec.get("type", "str")
        key = spec.get("key", "")
    else:
        path = spec
        vtype = "str"
        key = ""

    if not path:
        if vtype == "int":
            return None
        if vtype == "list":
            return []
        return ""

    node = record
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (IndexError, ValueError):
                node = None
                break
        else:
            node = None
            break
        if node is None:
            break

    if vtype == "list":
        if not isinstance(node, list):
            return []
        if key:
            return [
                str(it.get(key, "")).strip()
                for it in node
                if isinstance(it, dict) and it.get(key) not in (None, "")
            ]
        return [str(it) for it in node if it not in (None, "")]

    if vtype == "int":
        if isinstance(node, bool):
            return None
        try:
            return int(node) if node is not None else None
        except (TypeError, ValueError):
            return None

    return str(node) if node is not None else ""


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
                # 防止 uuid 为空导致文件名异常
                if not record.get("uuid"):
                    src = record.get("source_url", json.dumps(record, sort_keys=True))
                    record["uuid"] = _make_uuid(f"{site_key}:{src}")
                _record_parse_lineage(record, op=record.get("parse_method", "api"))
                yield record
                continue

        # 生成幂等 UUID
        uid_src = (
            _get_nested(record, uid_field) if uid_field
            else json.dumps(record, sort_keys=True)
        )
        doc_uuid = _make_uuid(f"{site_key}:{uid_src}")

        title = _get_nested(record, field_map.get("title", ""))
        publish_date = normalize_date(_get_nested(record, field_map.get("publish_date", "")))
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

        base_result = {
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

        # 保留额外字段（用于 hkex 等站点）
        for key in ["stock_code", "stock_name", "stock_id", "announcement_type"]:
            if key in record:
                base_result[key] = record[key]

        # extra_fields：按配置从原始记录抽取来源特有字段，写入 extra（供 doc_extra 落库）
        extra_fields = profile.get("extra_fields", {})
        if extra_fields:
            extra = {k: _extract_extra_value(record, v) for k, v in extra_fields.items()}
            if extra:
                base_result["extra"] = extra

        _record_parse_lineage(base_result, op=base_result["parse_method"])
        yield base_result
