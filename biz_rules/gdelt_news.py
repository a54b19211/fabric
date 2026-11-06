"""GDELT DOC新闻索引小批读取；保留元数据，不追抓第三方新闻全文。"""
from datetime import datetime, timezone
import time
from urllib.parse import urlsplit

import httpx

from biz_rules.history_models import ArchiveRecord, HistoryError, sha256, source_time
from collection import transport

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


def article_id(value):
    url = value.get("url")
    if not isinstance(url, str) or not url or len(url) > 8000:
        raise HistoryError("gdelt_article_url", "新闻索引缺少可核对的原网址")
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise HistoryError("gdelt_article_url", "新闻索引原网址格式不合法")
    return "url:" + sha256(url)  # 保留完整query；不同原网址不按标题合并。


def project_gdelt(item, data):
    if item.source_record_id != article_id(data):
        raise HistoryError("native_id_mismatch", "新闻索引编号与原网址不一致")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HistoryError("gdelt_article_title", "新闻索引缺少标题")
    observed = None
    if data.get("seendate"):
        try:
            observed = datetime.strptime(data["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            raise HistoryError("gdelt_seen_date", "GDELT观察时间无法核对") from None
    fields = ("url", "title", "seendate", "domain", "language", "sourcecountry")
    return {
        "record_kind": "document", "title": title, "source_url": data["url"],
        "clean_text": title, "facts": {key: data[key] for key in fields if key in data},
        "field_locators": {key: {"json_pointer": "/" + key} for key in fields if key in data},
        "source_published_at": None,
        "source_updated_at": source_time(observed.isoformat(), "gdelt_first_observed").model_dump(mode="json") if observed else None,
        "event_time": None, "parser_version": "datafabric.gdelt.doc.v1",
    }


def read_gdelt(request):
    from biz_rules.history_sources import parse_json, _raw_array_items
    if request.source_record_ids:
        raise HistoryError("by_id_unsupported", "GDELT索引暂不支持原编号点查；已采用资料可读固定版本")
    params = {"query": request.query, "mode": "artlist", "format": "json",
              "sort": "datedesc", "maxrecords": request.max_records}
    for field, key in (("updated_since", "startdatetime"), ("updated_until", "enddatetime")):
        value = getattr(request, field)
        if value:
            params[key] = value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    if not request.updated_since and not request.updated_until:
        params["timespan"] = "24h"
    records, seen, error, used, exhausted, stop = [], {}, None, 0, False, None
    started = time.monotonic()
    try:
        with httpx.Client(timeout=min(20, request.deadline_seconds), follow_redirects=False) as client:
            with client.stream("GET", ENDPOINT, params=params) as response:
                if response.status_code == 429:
                    raise HistoryError("source_rate_limited", "GDELT限速；此次未确认结果，保留任务供有界重试", 503)
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 8 * 1024 * 1024:
                        raise HistoryError("source_byte_budget", "新闻索引响应超过8MiB")
                    if time.monotonic() - started >= request.deadline_seconds:
                        raise HistoryError("source_deadline", "新闻索引读取超时")
            used = len(body)
            raw = body.decode("utf-8")
            data = parse_json(raw)
            if not isinstance(data.get("articles"), list):
                raise HistoryError("gdelt_response_shape", "新闻索引没有返回articles数组，不能当空结果")
            articles = list(_raw_array_items(raw, ["articles"]))
            if len(articles) > request.max_records:
                raise HistoryError("source_record_budget", "新闻索引超过声明的条数限制")
            for article, original in articles:
                native = article_id(article)
                item = ArchiveRecord(source_record_id=native, record_kind="document", raw_content=original)
                project_gdelt(item, article)
                if native in seen:
                    if seen[native] != item.raw_content:
                        records = [record for record in records if record.source_record_id != native]
                        raise HistoryError("ambiguous_native_id", "同批同网址返回不同内容，不能猜修订顺序")
                    continue
                seen[native] = item.raw_content
                records.append(item)
            exhausted = len(articles) < request.max_records
    except transport.Stop as stopped:
        stop = stopped.reason
    except HistoryError as exc:
        error = {"code": exc.code, "message": exc.message}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError):
        error = {"code": "source_read_failed", "message": "GDELT新闻索引读取失败；不能当作零新闻"}
    return {"records": records, "complete": False, "upstream_pagination_exhausted": exhausted and not (error or stop),
            "stop_reason": "source_failed" if error else stop or "bounded_index_response",
            "error": error, "requests": 1, "bytes": used, "source_total": None, "next_continuation": None}
