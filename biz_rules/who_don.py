"""WHO官方DON公告读取；保留原件，不将选择性公告当全球疫情统计。"""

from __future__ import annotations

import time
from uuid import UUID

import httpx
from bs4 import BeautifulSoup

from biz_rules.history_models import ArchiveRecord, HistoryError, canonical, source_time

ENDPOINT = "https://www.who.int/api/emergencies/diseaseoutbreaknews"
WHO_PARSER = "datafabric.who.don.projection.v0.2"
PATHS = {
    "native_notice_id": "/Id",
    "notice_id": "/DonId",
    "title": "/Title",
    "publication_date": "/PublicationDate",
    "publication_date_and_time": "/PublicationDateAndTime",
    "last_modified": "/LastModified",
    "summary": "/Summary",
    "overview": "/Overview",
    "epidemiology": "/Epidemiology",
    "response": "/Response",
    "advice": "/Advice",
    "assessment": "/Assessment",
    "further_information": "/FurtherInformation",
}


def project_who(item, data):
    from biz_rules.history_sources import _facts

    try:
        native = str(UUID(data["Id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HistoryError("who_notice_id", "WHO公告缺少合法原生Id") from None
    if item.source_record_id != native or item.record_kind != "document":
        raise HistoryError("native_id_mismatch", "WHO公告ID/类型与原件不一致")
    if not isinstance(data.get("Title"), str) or not data["Title"].strip():
        raise HistoryError("who_notice_title", "WHO公告缺少标题")
    for key in ("PublicationDate", "PublicationDateAndTime", "LastModified"):
        if data.get(key) is not None and source_time(data[key]) is None:
            raise HistoryError("who_notice_time", "WHO公告原生时间无效")
    facts, locators = _facts(data, PATHS)
    parts = [data["Title"]]
    sections = {}
    for key in (
        "Summary",
        "Overview",
        "Epidemiology",
        "Response",
        "Advice",
        "Assessment",
    ):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise HistoryError("who_notice_text", "WHO公告正文不是原生文本")
        if value:
            soup = BeautifulSoup(value, "html.parser")
            text = soup.get_text(" ", strip=True)
            blocks, cursor = [], 0
            for tag in soup.find_all(["p", "li"]):
                if tag.find_parent(["p", "li", "table", "figure", "figcaption", "caption"]):
                    continue
                block = tag.get_text(" ", strip=True)
                start = text.find(block, cursor) if block else -1
                if start >= 0:
                    blocks.append((start, start + len(block)))
                    cursor = start + len(block)
            if not soup.find(["p", "li", "table", "figure", "figcaption", "caption", "h1", "h2", "h3", "h4"]):
                blocks = None  # Plain text or inline markup, unlike excluded structural HTML.
            sections[key] = (text, len("\n\n".join(parts)) + 2, blocks)
            parts.append(text)
    clean_text = "\n\n".join(parts)
    from biz_rules.who_facts import extract_counts

    extracted = extract_counts(sections, native_notice_id=native, clean_text=clean_text)
    # 派生计数自带原生字段pointer与clean_text精确范围，不伪装成JSON内已有数值。
    facts["count_statements"] = extracted.pop("statements")
    facts["count_extraction"] = extracted
    slug = data.get("UrlName")
    url = None
    if isinstance(slug, str) and slug and all(c.isalnum() or c in "-_" for c in slug):
        url = "https://www.who.int/emergencies/disease-outbreak-news/item/" + slug
    published = source_time(
        data.get("PublicationDateAndTime") or data.get("PublicationDate"),
        "who_notice_publication",
    )
    return {
        "record_kind": "document",
        "title": data["Title"],
        "source_url": url,
        "clean_text": clean_text,
        "parser_version": WHO_PARSER,
        "facts": facts,
        "field_locators": locators,
        "event_time": published.model_dump(mode="json") if published else None,
        "source_published_at": published.model_dump(mode="json") if published else None,
        "source_updated_at": source_time(
            data.get("LastModified"), "who_notice_update"
        ).model_dump(mode="json")
        if data.get("LastModified")
        else None,
    }


def read_who(request):
    from biz_rules.history_sources import _raw_array_items, parse_json

    records, requests, used, error = [], 0, 0, None
    exhausted, stop, continuation = False, "record_budget", None
    started = time.monotonic()
    consumed = request.continuation.read_count if request.continuation else 0
    position = None
    if request.continuation:
        try:
            position = parse_json(request.continuation.cursor)
            if set(position) != {"modified", "id"}:
                raise ValueError()
            position["id"] = str(UUID(position["id"]))
            modified = source_time(position["modified"])
            if modified is None or modified.precision != "instant":
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise HistoryError(
                "who_cursor_mismatch", "WHO续页须为原生LastModified/Id定位"
            ) from None
    if request.source_record_ids:
        raise HistoryError(
            "by_id_unsupported", "WHO公开读取暂按明确时间窗/标题范围，不猜by-ID接口"
        )
    filters = []
    for key, sign in (("updated_since", "ge"), ("updated_until", "le")):
        value = getattr(request, key)
        if value:
            filters.append("LastModified " + sign + " " + value.isoformat())
    if request.query.strip() != "*":
        filters.append("contains(Title,'" + request.query.replace("'", "''") + "')")
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        try:
            for _ in range(request.max_pages):
                remaining = request.max_records - len(records)
                if remaining <= 0:
                    break
                size = min(remaining, 50)
                params = {
                    "$top": size + 1,
                    "$orderby": "LastModified asc,Id asc",
                }
                page_filters = list(filters)
                if position:
                    stamp, native = position["modified"], position["id"]
                    page_filters.append(
                        f"(LastModified gt {stamp} or (LastModified eq {stamp} and Id gt {native}))"
                    )
                if page_filters:
                    params["$filter"] = " and ".join(page_filters)
                requests += 1
                budget = request.deadline_seconds - (time.monotonic() - started)
                if budget <= 0:
                    raise HistoryError("source_deadline", "WHO公告读取超过时间预算")
                with client.stream(
                    "GET", ENDPOINT, params=params, timeout=min(20, budget)
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > request.deadline_seconds:
                            raise HistoryError(
                                "source_deadline", "WHO公告读取超过时间预算"
                            )
                        body.extend(chunk)
                        if used + len(body) > 8 * 1024 * 1024:
                            raise HistoryError(
                                "source_byte_budget", "WHO公告读取超过8MiB预算"
                            )
                used += len(body)
                raw = body.decode("utf-8")
                data = parse_json(raw)
                if not isinstance(data.get("value"), list):
                    raise HistoryError(
                        "who_response_shape", "WHO未返回公告数组，不能视为空结果"
                    )
                items = list(_raw_array_items(raw, ["value"]))
                selected = items[:size]
                for value, original in selected:
                    item = ArchiveRecord(
                        source_record_id=str(UUID(value["Id"])),
                        record_kind="document",
                        raw_content=original,
                    )
                    project_who(item, value)
                    records.append(item)
                consumed += len(selected)
                if selected:
                    last = selected[-1][0]
                    modified = source_time(last.get("LastModified"))
                    if modified is None or modified.precision != "instant":
                        raise HistoryError(
                            "who_cursor_mismatch", "WHO分页缺精确LastModified，不能推进"
                        )
                    next_position = {
                        "modified": modified.value,
                        "id": str(UUID(last["Id"])),
                    }
                    if position == next_position:
                        raise HistoryError(
                            "who_repeated_cursor", "WHO分页重复，不能视为EOF"
                        )
                    position = next_position
                exhausted = len(items) <= size
                continuation = (
                    None
                    if exhausted
                    else {
                        "scope_hash": request.scope_hash(),
                        "cursor": canonical(position),
                        "read_count": consumed,
                    }
                )
                stop = "upstream_exhausted" if exhausted else "record_budget"
                if exhausted or len(records) >= request.max_records:
                    break
            else:
                stop = "page_budget"
        except HistoryError as exc:
            error = {"code": exc.code, "message": exc.message}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError):
            error = {
                "code": "source_read_failed",
                "message": "WHO公告读取或格式校验失败；不当作零公告",
            }
    return {
        "records": records,
        "complete": False,
        "upstream_pagination_exhausted": exhausted and not error,
        "stop_reason": "source_failed" if error else stop,
        "error": error,
        "requests": requests,
        "bytes": used,
        "source_total": None,
        "next_continuation": continuation if not error else None,
    }
