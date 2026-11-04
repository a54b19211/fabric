"""SEC官方CompanyConcept原件：只解析原生披露，不选择可比期或估算财务数。

CompanyConcept和CompanyFacts属于SEC同一XBRL API族。本入口使用单概念完整JSON，
避免把多MiB整公司响应伪装成可装入单条History的原件。实际访问地址在来源目录和回执中明示。
"""

from __future__ import annotations

import os
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from biz_rules.history_models import ArchiveRecord, HistoryError

SEC_PARSER = "datafabric.sec.companyconcept.v0.1"
ENDPOINT = "https://data.sec.gov/api/xbrl/companyconcept/"
NATIVE_ID = re.compile(r"CIK([0-9]{10}):(us-gaap|ifrs-full|dei|srt):([A-Za-z][A-Za-z0-9]{0,119})")
ROW_FIELDS = ("val", "start", "end", "filed", "fy", "fp", "form", "accn", "frame")


def split_native(native):
    match = NATIVE_ID.fullmatch(native) if isinstance(native, str) else None
    if match is None or int(match.group(1)) == 0:
        raise HistoryError("sec_native_id", "SEC原生ID须为CIK十位数:taxonomy:tag")
    return match.groups()


def source_url(native):
    cik, taxonomy, tag = split_native(native)
    return ENDPOINT + "CIK" + cik + "/" + taxonomy + "/" + tag + ".json"


def _day(value, field):
    if not isinstance(value, str):
        raise HistoryError("sec_fact_date", "SEC事实日期须为原生YYYY-MM-DD: " + field)
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise HistoryError("sec_fact_date", "SEC事实日期格式无效: " + field) from None


def financial_rows(data):
    """原生units逐行展开；保留重复披露、原单位、期间和字段位置，不做latest选择。"""
    units = data.get("units")
    if not isinstance(units, dict) or not units:
        raise HistoryError("sec_fact_units", "SEC概念缺少原生单位数组")
    observations = []
    for unit, rows in units.items():
        if not isinstance(unit, str) or not unit or len(unit) > 120 or not isinstance(rows, list):
            raise HistoryError("sec_fact_units", "SEC原生单位/事实数组无效")
        escaped_unit = unit.replace("~", "~0").replace("/", "~1")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise HistoryError("sec_fact_shape", "SEC事实须为对象")
            value = row.get("val")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise HistoryError("sec_fact_value", "SEC val须为来源数值；缺失不补零")
            try:
                finite = Decimal(str(value)).is_finite()
            except InvalidOperation:
                finite = False
            if not finite:
                raise HistoryError("sec_fact_value", "SEC val须为有限数值")
            for field in ("end", "filed"):
                _day(row.get(field), field)
            if "start" in row:
                _day(row["start"], "start")
                if row["start"] > row["end"]:
                    raise HistoryError("sec_fact_date", "SEC事实起止日期颠倒")
            if not isinstance(row.get("accn"), str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", row["accn"]):
                raise HistoryError("sec_fact_accession", "SEC事实缺少原生accession")
            if not isinstance(row.get("form"), str) or not row["form"]:
                raise HistoryError("sec_fact_form", "SEC事实缺少披露表格类型")
            if row.get("fy") is not None and (not isinstance(row["fy"], int) or isinstance(row["fy"], bool)):
                raise HistoryError("sec_fact_year", "SEC fy须为来源财政年，不能替代期间")
            for field in ("fp", "frame"):
                if row.get(field) is not None and not isinstance(row[field], str):
                    raise HistoryError("sec_fact_shape", "SEC期间/框架字段类型无效")
            pointer = "/units/" + escaped_unit + "/" + str(index)
            observations.append({
                "unit": unit,
                **{field: row[field] for field in ROW_FIELDS if field in row},
                "source_row_pointer": pointer,
                "field_locators": {
                    field: {"json_pointer": pointer + "/" + field}
                    for field in ROW_FIELDS if field in row
                },
                # 单位是JSON对象键，不能伪造一个不存在的/unit值指针。
                "unit_locator": {"json_pointer": "/units", "object_key": unit},
            })
    return observations


def project_sec(item, data):
    from biz_rules.history_sources import _facts

    cik, taxonomy, tag = split_native(item.source_record_id)
    if (item.record_kind != "document" or not isinstance(data.get("cik"), int)
            or isinstance(data["cik"], bool) or data["cik"] != int(cik)
            or data.get("taxonomy") != taxonomy or data.get("tag") != tag):
        raise HistoryError("native_id_mismatch", "SEC原生CIK/概念/类型与完整原件不一致")
    if any(not isinstance(data.get(field), str) or not data[field].strip()
           for field in ("entityName", "label", "description")):
        raise HistoryError("sec_concept_metadata", "SEC概念缺少原生企业、标签或定义")
    facts, locators = _facts(data, {
        "cik": "/cik", "entity_name": "/entityName", "taxonomy": "/taxonomy",
        "concept": "/tag", "concept_label": "/label", "concept_description": "/description",
        "units": "/units",
    })
    rows = financial_rows(data)
    # financial_observations是明确的确定性展开；每个数/期间仍指回原始units数组。
    facts["financial_observations"] = rows
    lines = [data["entityName"] + " — " + data["label"], data["description"]]
    for row in rows:
        lines.append(" ".join([row["unit"], *[field + "=" + str(row[field]) for field in ROW_FIELDS if field in row]]))
    return {
        "parser_version": SEC_PARSER,
        "record_kind": "document", "title": lines[0],
        "source_url": source_url(item.source_record_id), "clean_text": "\n".join(lines),
        "facts": facts, "field_locators": locators,
        # 多期/多次申报的集合没有唯一事件日期；不能把最早/最新filed冒充整个原件的可得时间。
        "event_time": None, "source_published_at": None, "source_updated_at": None,
    }


def read_sec(request):
    from biz_rules.history_sources import _bounded_text, parse_json

    if request.query.strip() != "*" or not request.source_record_ids:
        raise HistoryError("sec_explicit_scope", "SEC读取须query='*'并给明确CIK:taxonomy:tag原生ID清单")
    if request.continuation is not None or request.updated_since or request.updated_until:
        raise HistoryError("sec_explicit_scope", "SEC单概念原件按固定清单重查，不接外部游标或日期过滤")
    selected = list(dict.fromkeys(request.source_record_ids))
    for native in selected:
        split_native(native)
    records, used, requests, error = [], 0, 0, None
    started, stop = time.monotonic(), "record_budget"
    exhausted = False
    user_agent = os.environ.get("DATAFABRIC_SEC_USER_AGENT", "BioRadar-DataFabric/1.0 (bounded academic research)")
    if not user_agent.strip() or any(ord(c) < 32 for c in user_agent):
        raise HistoryError("sec_user_agent", "SEC读取须有明确有效的工具User-Agent")
    with httpx.Client(timeout=20, follow_redirects=False, headers={"User-Agent": user_agent, "Accept": "application/json"}) as client:
        try:
            budget_count = min(len(selected), request.max_records, request.max_pages)
            for index, native in enumerate(selected[:budget_count]):
                # 间隔由共享限速组 sec.gov 控制（每秒一请求，见来源目录），_bounded_text 内过闸
                if time.monotonic() - started >= request.deadline_seconds:
                    stop = "deadline"
                    break
                requests += 1
                raw = _bounded_text(client, source_url(native), {}, started,
                                    request.deadline_seconds, 8 * 1024 * 1024 - used)
                used += len(raw.encode("utf-8"))
                item = ArchiveRecord(source_record_id=native, record_kind="document", raw_content=raw)
                project_sec(item, parse_json(raw))
                records.append(item)
            else:
                exhausted = len(records) == len(selected)
                stop = "upstream_exhausted" if exhausted else "page_budget" if budget_count == request.max_pages else "record_budget"
        except HistoryError as exc:
            error = {"code": exc.code, "message": exc.message}
            stop = "source_failed"
        except (httpx.HTTPError, ValueError, TypeError, KeyError, UnicodeError):
            error = {"code": "source_read_failed", "message": "SEC读取或原件校验失败；不当作无披露或零值"}
            stop = "source_failed"
    return {
        "records": records, "complete": False,
        "upstream_pagination_exhausted": exhausted and not error,
        "stop_reason": stop, "error": error, "requests": requests, "bytes": used,
        "source_total": None, "next_continuation": None,
    }
