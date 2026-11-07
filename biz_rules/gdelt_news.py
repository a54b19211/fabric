"""GDELT DOC新闻索引小批读取；保留元数据，不追抓第三方新闻全文。

The index has no cursor and no total, so a response that reaches max_records may hide the rest of its window.
A read with both bounds is cut at UTC hour marks. A window whose response is full is halved and both halves are
read again, down to FINEST_WINDOW; one query-hour costs at most 1 + 2 + 4 = 7 requests. A response below the cap
only says that this system's cap was not hit, never that the window is whole. Every window leaves a receipt in the
run's coverage; the ones still full at the finest size are marked possibly_truncated.
"""
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx

from biz_rules.history_models import ArchiveRecord, HistoryError, sha256, source_time
from collection import transport

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
FINEST_WINDOW = timedelta(minutes=15)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


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


class Window(NamedTuple):
    """A range of seendate to read. A bound is None only for a request that did not give both bounds."""

    start: datetime | None
    end: datetime | None

    def splittable(self) -> bool:
        """Both halves must still be at least FINEST_WINDOW long."""
        return self.start is not None and self.end is not None and self.end - self.start >= 2 * FINEST_WINDOW

    def halves(self) -> tuple["Window", "Window"]:
        middle = self.start + timedelta(seconds=int((self.end - self.start).total_seconds()) // 2)
        return Window(self.start, middle), Window(middle, self.end)


@dataclass
class WindowReceipt:
    """One window of one query: what came back, or why nothing did.

    status: read (a response below the cap, or a full one that cannot be cut), split (a full response, its halves
    follow), failed (the request failed: no count, not zero), not_read (the read stopped before this window).
    full: the response reached max_records. possibly_truncated: a full response that cannot be cut any further."""

    start: str | None
    end: str | None
    status: str
    returned: int | None = None
    distinct_urls: int | None = None
    full: bool = False
    possibly_truncated: bool = False
    requests: int = 0
    reason: str | None = None


def _iso(value):
    return value.isoformat() if value else None


def _receipt(window, status, **fields):
    return WindowReceipt(_iso(window.start), _iso(window.end), status, **fields)


def _sent() -> int:
    """Requests the read's budget has counted so far, retries of a 429 or a timeout included."""
    budget = transport.current()
    return budget.requests if budget else 0


def _params(request, window):
    params = {"query": request.query, "mode": "artlist", "format": "json",
              "sort": "datedesc", "maxrecords": request.max_records}
    for value, key in ((window.start, "startdatetime"), (window.end, "enddatetime")):
        if value:
            params[key] = value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    if window.start is None and window.end is None:
        params["timespan"] = "24h"
    return params


def _pieces(request):
    """(window, the range after it) pairs: the requested range cut at UTC hour marks, the first and the last piece
    may be shorter. A request without both bounds is one piece, which cannot be cut."""
    since, until = request.updated_since, request.updated_until
    if since is None or until is None:
        yield Window(since, until), None
        return
    cursor, until = since.astimezone(timezone.utc), until.astimezone(timezone.utc)
    while True:
        end = min(until, cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        yield Window(cursor, end), (Window(end, until) if end < until else None)
        if end >= until:
            return
        cursor = end


class _Read:
    """One read of the index: the unique articles so far, a receipt per window and the windows still to read."""

    def __init__(self, request):
        self.request = request
        self.started = time.monotonic()
        self.records, self.seen, self.receipts = [], {}, []
        self.queue, self.after = [], None  # the current piece's windows to read (next one last); the range after it
        self.in_flight = None  # (window, requests counted before it) while its request is out
        self.bytes = 0

    def run(self, client):
        for piece, after in _pieces(self.request):
            self.queue, self.after = [piece], after
            while self.queue:
                self._read_window(client, self.queue.pop())

    def _read_window(self, client, window):
        self.in_flight = (window, _sent())
        articles = self._fetch(client, window)
        distinct = self._take(articles)
        requests = _sent() - self.in_flight[1]
        self.in_flight = None
        full = len(articles) >= self.request.max_records
        counts = {"returned": len(articles), "distinct_urls": distinct, "full": full, "requests": requests}
        if full and window.splittable():
            self.receipts.append(_receipt(window, "split", **counts))
            self.queue.extend(reversed(window.halves()))  # the earlier half is read first
            return
        reason = None
        if full:
            reason = "finest_window_full" if window.start and window.end else "unbounded_window"
        self.receipts.append(_receipt(window, "read", possibly_truncated=full, reason=reason, **counts))

    def _fetch(self, client, window):
        """One request for the window: its articles as (parsed, original text) pairs."""
        from biz_rules.history_sources import _raw_array_items, parse_json
        request = self.request
        with client.stream("GET", ENDPOINT, params=_params(request, window)) as response:
            if response.status_code == 429:
                raise HistoryError("source_rate_limited", "GDELT限速；此次未确认结果，保留任务供有界重试", 503)
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise HistoryError("source_byte_budget", "新闻索引响应超过8MiB")
                if time.monotonic() - self.started >= request.deadline_seconds:
                    raise HistoryError("source_deadline", "新闻索引读取超时")
        self.bytes += len(body)
        raw = body.decode("utf-8")
        if not isinstance(parse_json(raw).get("articles"), list):
            raise HistoryError("gdelt_response_shape", "新闻索引没有返回articles数组，不能当空结果")
        articles = list(_raw_array_items(raw, ["articles"]))
        if len(articles) > request.max_records:
            raise HistoryError("source_record_budget", "新闻索引超过声明的条数限制")
        return articles

    def _take(self, articles):
        """Keep the first copy of each url; the number of distinct urls in this response."""
        urls = set()
        for article, original in articles:
            native = article_id(article)
            item = ArchiveRecord(source_record_id=native, record_kind="document", raw_content=original)
            project_gdelt(item, article)
            urls.add(native)
            if native in self.seen:
                if self.seen[native] != item.raw_content:
                    self.records = [record for record in self.records if record.source_record_id != native]
                    raise HistoryError("ambiguous_native_id", "同批同网址返回不同内容，不能猜修订顺序")
                continue
            self.seen[native] = item.raw_content
            self.records.append(item)
        return len(urls)

    def account_for_unfinished(self, error, stop):
        """Receipts for what a failed or stopped read did not finish: the window in flight, the queued ones, the rest."""
        if not (error or stop):
            return
        if self.in_flight:
            window, before = self.in_flight
            self.receipts.append(_receipt(window, "failed" if error else "not_read", requests=_sent() - before,
                                          reason=error["code"] if error else stop))
        skipped = stop or "earlier_window_failed"
        for window in reversed(self.queue):
            self.receipts.append(_receipt(window, "not_read", reason=skipped))
        if self.after:
            self.receipts.append(_receipt(self.after, "not_read", reason=skipped))

    def batch(self, error, stop):
        truncated = [receipt for receipt in self.receipts if receipt.possibly_truncated]
        return {
            "records": self.records, "complete": False,
            "upstream_pagination_exhausted": not (truncated or error or stop),
            "stop_reason": "source_failed" if error else stop or "bounded_index_response",
            "error": error, "requests": sum(receipt.requests for receipt in self.receipts), "bytes": self.bytes,
            "source_total": None, "next_continuation": None,
            "window_coverage": [asdict(receipt) for receipt in self.receipts],
            "truncated_intervals": [{"start": receipt.start, "end": receipt.end} for receipt in truncated],
        }


def read_gdelt(request):
    if request.source_record_ids:
        raise HistoryError("by_id_unsupported", "GDELT索引暂不支持原编号点查；已采用资料可读固定版本")
    read = _Read(request)
    error = stop = None
    try:
        with httpx.Client(timeout=min(20, request.deadline_seconds), follow_redirects=False) as client:
            read.run(client)
    except transport.Stop as stopped:
        stop = stopped.reason
    except HistoryError as exc:
        error = {"code": exc.code, "message": exc.message}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError):
        error = {"code": "source_read_failed", "message": "GDELT新闻索引读取失败；不能当作零新闻"}
    read.account_for_unfinished(error, stop)
    return read.batch(error, stop)
