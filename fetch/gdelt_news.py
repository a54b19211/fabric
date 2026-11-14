"""GDELT DOC新闻索引小批读取；保留元数据，不追抓第三方新闻全文。

The index has no cursor and no total, so a response that reaches max_records may hide the rest of its window.
A read with both bounds is cut at UTC hour marks. A window whose response is full is halved and both halves are
read again, down to FINEST_WINDOW; one query-hour costs at most 1 + 2 + 4 = 7 requests. A response below the cap
only says that this system's cap was not hit, never that the window is whole. Every window leaves a receipt in the
run's coverage; the ones still full at the finest size are marked possibly_truncated.

GDELT asks for one request every 5 seconds (the catalog spaces them 6 s apart) and takes 12-20 s to answer. It answers
429 to most requests whatever the spacing (measured 2026-11-13 from zktest, direct and through the proxy, gaps of 6 s to
5 minutes, about 80% of 160 requests; see fetch/catalog.py), so a request is not asked once but up to TRIES times: after
each 429 the read waits the interval the answer names and sends the request again. After the last 429, or when there is
no time left for the wait, the read ends as a failure that says so.
"""
import re
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx

from fetch import transport
from store.record_models import ArchiveRecord, FabricError, sha256, source_time

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
FINEST_WINDOW = timedelta(minutes=15)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RESPONSE_SECONDS = 45  # GDELT needs 12-20 s to answer a query; a 20 s limit cut some of them off
DEFAULT_429_INTERVAL = 5  # what GDELT asks for when its 429 does not name an interval
TRIES = 6  # how many times a request is sent when GDELT keeps answering 429 (about 18 s each: they fit a 120 s read)
RETRY_MARGIN = 1  # seconds added to the interval GDELT names


def article_id(value):
    url = value.get("url")
    if not isinstance(url, str) or not url or len(url) > 8000:
        raise FabricError("gdelt_article_url", "新闻索引缺少可核对的原网址")
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise FabricError("gdelt_article_url", "新闻索引原网址格式不合法")
    return "url:" + sha256(url)  # 保留完整query；不同原网址不按标题合并。


def project_gdelt(item, data):
    if item.source_record_id != article_id(data):
        raise FabricError("native_id_mismatch", "新闻索引编号与原网址不一致")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise FabricError("gdelt_article_title", "新闻索引缺少标题")
    observed = None
    if data.get("seendate"):
        try:
            observed = datetime.strptime(data["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            raise FabricError("gdelt_seen_date", "GDELT观察时间无法核对") from None
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


def _interval_asked(answer: str) -> int:
    """Seconds between requests that a 429 asks for: its text says "limit requests to one every 5 seconds"."""
    found = re.search(r"one every (\d+) second", answer)
    return int(found[1]) if found else DEFAULT_429_INTERVAL


def _iso(value):
    return value.isoformat() if value else None


def _receipt(window, status, **fields):
    """一个窗口这次读回了什么：status 是 read（读到了）、split（满了，拆成两半再读）、failed（请求失败，没有条数）、
    not_read（读取在它之前停了）；possibly_truncated：满了又不能再拆，可能漏了线索。"""
    return {"start": _iso(window.start), "end": _iso(window.end), "status": status, "returned": None,
            "distinct_urls": None, "full": False, "possibly_truncated": False, "requests": 0, "reason": None,
            **fields}


def _sent() -> int:
    """Requests sent so far in this read (fetch.transport counts them), the resends after a 429 included."""
    return transport.SENT["requests"]


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
        self.deadline = self.started + request.deadline_seconds
        self.records, self.seen, self.receipts = [], {}, []
        self.queue, self.after = [], None  # the current piece's windows to read (next one last); the range after it
        self.in_flight = None  # (window, requests counted before it) while its request is out

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
        """One request for the window: its articles as (parsed, original text) pairs. A 429 is answered by waiting the
        interval GDELT names and asking again, up to TRIES times (so transport.get sends each request only once)."""
        for attempt in range(1, TRIES + 1):
            response = transport.get(ENDPOINT, _params(self.request, window), client=client, cap=MAX_RESPONSE_BYTES,
                                     timeout=min(RESPONSE_SECONDS, self.request.deadline_seconds), tries=1,
                                     deadline=self.deadline)
            if response.status_code != 429:
                return self._articles(response)
            asked = _interval_asked(response.text)
            if attempt == TRIES:
                raise FabricError("source_rate_limited", f"对方限流：GDELT 返回 429，按它要求的间隔（{asked} 秒）等过再发，"
                                   f"共发 {TRIES} 次仍是 429；请隔一段时间再读", 503)
            wait = asked + RETRY_MARGIN
            if time.monotonic() - self.started + wait >= self.request.deadline_seconds:
                raise FabricError("source_rate_limited", f"对方限流：GDELT 返回 429，要求间隔 {asked} 秒，期限内等不到重试；"
                                   "请隔一段时间再读", 503)
            time.sleep(wait)

    def _articles(self, response):
        """The articles of one answer, as (parsed, original text) pairs."""
        from fetch.projection import parse_json
        from fetch.sources import _raw_array_items
        request = self.request
        response.raise_for_status()
        raw = response.content.decode("utf-8")
        found = parse_json(raw)
        if found == {}:  # GDELT's answer to a window without a single article: 200 with the body {} (measured 2026-11-13)
            return []
        if not isinstance(found.get("articles"), list):
            raise FabricError("gdelt_response_shape", "新闻索引没有返回articles数组，不能当空结果")
        articles = list(_raw_array_items(raw, ["articles"]))
        if len(articles) > request.max_records:
            raise FabricError("source_record_budget", "新闻索引超过声明的条数限制")
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
                    raise FabricError("ambiguous_native_id", "同批同网址返回不同内容，不能猜修订顺序")
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
        truncated = [receipt for receipt in self.receipts if receipt["possibly_truncated"]]
        return {
            "records": self.records, "complete": False,
            "upstream_pagination_exhausted": not (truncated or error or stop),
            "stop_reason": "source_failed" if error else stop or "bounded_index_response",
            "error": error, "source_total": None, "next_continuation": None,
            "window_coverage": self.receipts,
            "truncated_intervals": [{"start": receipt["start"], "end": receipt["end"]} for receipt in truncated],
        }


def read_gdelt(request):
    if request.source_record_ids:
        raise FabricError("by_id_unsupported", "GDELT索引暂不支持原编号点查；已采用资料可读固定版本")
    read = _Read(request)
    error = stop = None
    try:
        with httpx.Client(follow_redirects=False) as client:
            read.run(client)
    except transport.Stop as stopped:
        stop = stopped.reason
    except FabricError as exc:
        error = {"code": exc.code, "message": exc.message}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError):
        error = {"code": "source_read_failed", "message": "GDELT新闻索引读取失败；不能当作零新闻"}
    read.account_for_unfinished(error, stop)
    return read.batch(error, stop)
