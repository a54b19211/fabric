"""旧 ETL 的网站采集（NMPA / CDE / ChiCTR）并入唯一框架的插件 legacy_site_crawler。

复用旧代码，不重写：
- 站点配置 config/sites/*（列表页、选择器、翻页、详情提取器）原样使用；
- 列表翻页：core_engine.pagination_driver.make_driver；
- 详情页抓取与解析：core_engine.scrapling_client.fetch_page + core_engine.router.route_webpage；
- 附件下载：core_engine.scrapling_client.download_pdf（原件按 sha256 命名落盘、fsync、不覆盖旧文件）。

和旧 ETL 的区别只在落库方式：不再写 zk.documents、不把附件文字合并进父文档，而是
- 详情页 → 一条 web_capture 版本（原始 HTML + 旧提取器的解析结果 + 附件清单）；
- 每个附件 → 一条独立的 attachment 版本（链接、父页面、sha256、大小、类型、原件路径），
  正文由加工层 processing.attachment_parse（自部署 MinerU 等）按版本产出。
附件下载失败只记在父页面的附件清单里（status=download_failed），父页面照常保存。
附件记录身份是“父页面#attachment=附件链接”（attachment_record_id）。

三种读取方式（FetchRequest）：
- query="by_url"，source_record_ids=详情页 URL：重抓这些页面及其附件；
- query=站点配置里的某个 start_url：按旧翻页驱动列出详情页，读 max_records 条，可续页；
- query="legacy_attachments"，source_record_ids=旧库父文档 URL：按旧库 attachments 表记录的链接
  重新下载附件原件（父页面不重抓；旧库合并的文字仍在 legacy 桥里）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from biz_rules.history_models import ArchiveRecord, HistoryError, source_time
from collection import rate
from collection.catalog import spec

WEB_SCHEMA = "datafabric.web_capture.v1"
ATT_SCHEMA = "datafabric.attachment.v1"
SITE_SOURCES = frozenset({"nmpa", "cde", "chictr"})
HTML_INLINE_BYTES = 700 * 1024

from config.crawler_settings import SITE_PROFILES  # noqa: E402  旧站点配置，原样复用


# ——旧代码入口（测试替换这几个名字）——
def _fetch_page(url, **kwargs):
    from core_engine.scrapling_client import fetch_page

    return fetch_page.fn(url, **kwargs)


def _route_webpage(page_data, site_key=None):
    from core_engine.router import route_webpage

    return route_webpage.fn(page_data, site_key=site_key)


def _download(pdf_url, parent_uuid="", source_site="", force=False, cookies=None):
    from core_engine.scrapling_client import download_pdf

    return download_pdf.fn(pdf_url, parent_uuid=parent_uuid, source_site=source_site, force=force, cookies=cookies)


def _make_driver(site, profile, start_url):
    from core_engine.pagination_driver import make_driver

    return make_driver(site, profile, start_url)


def _store_root() -> Path:
    from config.settings import APP

    return Path(APP.RAW_DIR)


@lru_cache(maxsize=1)
def _legacy_engine():
    from sqlalchemy import create_engine

    from config.settings import APP

    return create_engine(APP.DB_URL, pool_pre_ping=True, connect_args={"options": "-c default_transaction_read_only=on"})


def _legacy_attachment_links(site, parent_url):
    """旧库 attachments 表里这个父文档的附件链接（只读、有界）。旧 uuid = uuid5(URL)。"""
    from sqlalchemy import text

    from core_engine.scrapling_client import make_uuid

    with _legacy_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='10s'"))
        rows = conn.execute(
            text(
                "SELECT a.file_url, a.file_name FROM attachments a JOIN documents d ON d.uuid=a.parent_uuid "
                "WHERE a.parent_uuid=:uuid AND d.source_site=:site AND NOT a.is_deleted AND a.file_url<>'' "
                "ORDER BY a.id LIMIT 50"
            ),
            {"uuid": make_uuid(parent_url), "site": site},
        ).all()
    return [{"url": r[0], "text": r[1] or ""} for r in rows]


def attachment_record_id(parent_id: str, url: str) -> str:
    """附件记录身份 = 父页面 + 附件链接。同一文件挂在两个页面下（NMPA 同一公告在两个栏目各有地址）是两条
    稳定记录，不会来回换版本；同一份文件的原件按 sha256 只存一份，解析结果也按 sha256 复用。"""
    rid = parent_id + "#attachment=" + url
    if len(rid) > 400:
        rid = parent_id + "#attachment-sha256=" + hashlib.sha256(url.encode()).hexdigest()
    return rid


def _sha256_file(path: str) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _on_site(site, url) -> bool:
    host = (urlparse(url).hostname or "").lower()
    pattern = SITE_PROFILES[site].get("url_pattern", "").split("/")[0].lower()
    return bool(host) and (host == pattern or host.endswith("." + pattern) or host == "www." + pattern)


def _record(record_id, data) -> ArchiveRecord:
    return ArchiveRecord(
        source_record_id=record_id,
        record_kind="document",
        raw_content=json.dumps(data, ensure_ascii=False, sort_keys=True),
    )


class _Stop(Exception):
    """预算用完：reason 是 stop_reason。已读到的材料保留，调用方给出续读位置。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Resume(_Stop):
    """预算在某一父项的附件中途用完：attachment 是下一个要读的附件序号；page_recorded 表示这一页的
    页面版本已经保存，续读时只为拿附件链接和会话重取页面，不再占版本名额（Codex r61：否则 max_records=1
    时每轮都被父页面占满，永远读不到附件）。"""

    def __init__(self, reason, attachment, page_recorded=False):
        super().__init__(reason)
        self.attachment = attachment
        self.page_recorded = page_recorded


def run_byte_budget() -> int:
    return int(os.environ.get("DATAFABRIC_SITE_RUN_MAX_BYTES", str(256 * 1024 * 1024)))


class _Reader:
    """一次读取的预算（Codex r59）：
    - 版本数 ≤ max_records（父页面和附件都算）；
    - 对外请求数 ≤ max_pages × 10（列表页、详情页、附件下载都算，旧翻页驱动内部的请求也算）；
    - 总字节 ≤ DATAFABRIC_SITE_RUN_MAX_BYTES，单个附件 ≤ DATAFABRIC_ATTACHMENT_MAX_BYTES；
    - 截止时间 deadline_seconds。
    任何一项用完就停：已读到的保留，stop_reason 写明是哪一项，给续读位置，不声称读到底。"""

    def __init__(self, request):
        self.request = request
        self.site = request.source_id
        self.profile = SITE_PROFILES[self.site]
        self.spec = spec(self.site)
        self.began = time.monotonic()
        self.deadline = self.began + request.deadline_seconds
        self.records: list[ArchiveRecord | None] = []
        self.requests = 0
        self.request_budget = request.max_pages * 10
        self.bytes = 0
        self.byte_budget = run_byte_budget()
        # 本次读取的附件覆盖情况（stored / download_failed / too_large / not_attempted ...）。放在读取结果里，
        # 不放进页面原件：原件只放来源给出的内容，否则同一页面每重试一次就因状态不同多一个版本（Codex r61）。
        self.coverage: list[dict] = []

    def before_request(self):
        if self.requests >= self.request_budget:
            raise _Stop("request_budget")
        if self.bytes >= self.byte_budget:
            raise _Stop("byte_budget")
        try:
            rate.wait(self.spec.rate_group, self.spec.min_interval_seconds, deadline=self.deadline)
        except TimeoutError:
            raise _Stop("deadline") from None
        self.requests += 1

    def before_record(self):
        if len(self.records) >= self.request.max_records:
            raise _Stop("record_budget")

    def attachment(self, link, parent_id, cookies, discovered):
        """下载一个附件；返回父页面附件清单里的一项，成功时同时加一条附件版本。预算用完抛 _Stop。"""
        url = (link or {}).get("url") or ""
        record_id = attachment_record_id(parent_id, url)
        entry = {"record_id": record_id, "url": url, "text": (link or {}).get("text", ""), "status": "stored"}
        self.before_record()
        self.before_request()
        try:
            info = _download(url, parent_uuid=parent_id, source_site=self.site, cookies=cookies)
            sha, size = _sha256_file(info["local_path"])
        except ValueError as exc:
            status = "too_large" if str(exc) == "attachment_too_large" else "download_failed"
            entry.update(status=status, error=str(exc)[:80])
            self.coverage.append({"parent_record_id": parent_id, **entry})
            return entry
        except Exception as exc:  # noqa: BLE001 -- 附件失败是覆盖缺口，不让父页面失败
            entry.update(status="download_failed", error=type(exc).__name__)
            self.coverage.append({"parent_record_id": parent_id, **entry})
            return entry
        self.bytes += size
        entry.update(sha256=sha, size=size)
        self.records.append(
            _record(
                record_id,
                {
                    "schema_version": ATT_SCHEMA,
                    "role": "attachment",
                    "site": self.site,
                    "url": url,
                    "parent_record_id": parent_id,
                    "file_name": entry["text"] or Path(urlparse(url).path).name,
                    "file_ext": (info.get("file_ext") or "").lower(),
                    "sha256": sha,
                    "size": size,
                    "store_path": info["local_path"],
                    "discovered_from": discovered,
                    "status": "stored",
                },
            )
        )
        self.coverage.append({"parent_record_id": parent_id, **entry})
        return entry

    def page(self, url, skip_attachments=0, record_page=True):
        """读一个详情页和它的附件。skip_attachments：续读时这一页前面已读过的附件数；record_page=False：
        这一页的页面版本上一轮已保存，这次只为拿附件链接和会话重取，不再占版本名额。
        预算在附件中途用完时，父页面照常保存，抛 _Resume（续读位置指向下一个附件）。"""
        cfg = self.profile
        if record_page:
            self.before_record()
        self.before_request()
        page_data = _fetch_page(
            url,
            delay=0,  # 间隔由共享限速组统一控制
            wait_selector=cfg.get("detail_wait_selector"),
            disable_resources=cfg.get("disable_resources", True),
            solve_cloudflare=cfg.get("solve_cloudflare", False),
            use_simple_fetch=cfg.get("use_simple_fetch", False),
        )
        parsed = _route_webpage(page_data, site_key=self.site)
        html = getattr(page_data["page"], "html_content", "") or ""
        raw = html.encode("utf-8")
        html_sha = hashlib.sha256(raw).hexdigest()
        self.bytes += len(raw)
        capture = {
            "schema_version": WEB_SCHEMA,
            "role": "web_capture",
            "site": self.site,
            "url": url,
            "html_sha256": html_sha,
            "parsed": {k: parsed.get(k) for k in ("title", "publish_date", "markdown", "parse_method", "extra")},
            "attachments": [],
        }
        if len(raw) <= HTML_INLINE_BYTES:
            capture["html"] = html
        else:
            path = _store_root() / self.site / "html" / (html_sha + ".html")
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(raw)
            capture["html_store_path"] = str(path)
        index = len(self.records)
        if record_page:
            self.records.append(None)  # 占位：父页面排在它的附件前面
        cookies = page_data.get("cookies") or []
        links = [link for link in (parsed.get("attachment_links") or []) if link and link.get("url")]
        # 原件里的附件清单只有来源给出的链接和文字；每个附件这次读没读到记在 coverage
        capture["attachments"] = [
            {"record_id": attachment_record_id(url, link["url"]), "url": link["url"], "text": link.get("text", "")}
            for link in links
        ]
        if record_page:
            self.records[index] = _record(url, capture)
        for position, link in enumerate(links):
            if position < skip_attachments:
                continue
            try:
                self.attachment(link, url, cookies, "detail_page")
            except _Stop as stop:
                for later in links[position:]:
                    self.coverage.append({"parent_record_id": url, "url": later["url"], "status": "not_attempted",
                                          "record_id": attachment_record_id(url, later["url"])})
                raise _Resume(stop.reason, position, page_recorded=True) from None


@contextmanager
def _gated_driver(reader):
    """旧翻页驱动内部用 fetch_page 取列表页：读取期间让它也过同一个闸门、计入请求预算。
    js_click 型驱动（CDE）在一个浏览器会话里点击翻页，不经过 fetch_page，只按站点配置的间隔放慢。"""
    import core_engine.pagination_driver as driver

    real = driver.fetch_page

    class Gated:
        @staticmethod
        def fn(*args, **kwargs):
            reader.before_request()
            kwargs["delay"] = 0
            return real.fn(*args, **kwargs)

    driver.fetch_page = Gated
    try:
        yield
    finally:
        driver.fetch_page = real


def _cursor(request):
    if not request.continuation:
        return 0, 0, False
    try:
        value = json.loads(request.continuation.cursor)
        return int(value["i"]), int(value["a"]), bool(value.get("p"))
    except (ValueError, KeyError, TypeError):
        raise HistoryError("invalid_cursor", "续读位置无效") from None


def read_site(request) -> dict:
    """legacy_site_crawler 插件入口；返回值形状与 read_public 其他插件相同。
    续读位置 cursor = {"i": 第几个父项（详情页 / 旧库父文档 / 列表里第几个详情页）, "a": 该父项下第几个附件}。"""
    reader = _Reader(request)
    error, stop, exhausted, resume = None, "upstream_exhausted", False, None
    position = 0
    try:
        start_i, start_a, start_recorded = _cursor(request)
        position = start_i
        if request.query in {"by_url", "legacy_attachments"}:
            for url in request.source_record_ids:
                if not _on_site(reader.site, url):
                    raise HistoryError("url_outside_site", "链接不属于该来源站点")
            for position in range(start_i, len(request.source_record_ids)):
                url = request.source_record_ids[position]
                skip = start_a if position == start_i else 0
                if request.query == "by_url":
                    reader.page(url, skip_attachments=skip,
                                record_page=not (position == start_i and start_recorded))
                    continue
                for k, link in enumerate(_legacy_attachment_links(reader.site, url)):
                    if k < skip:
                        continue
                    try:
                        reader.attachment(link, url, None, "legacy_attachments_table")
                    except _Stop as stop_here:
                        raise _Resume(stop_here.reason, k, page_recorded=False) from None
            exhausted = True
        else:
            if request.query not in reader.profile.get("start_urls", []):
                raise HistoryError("unknown_start_url", "列表入口不在该站点配置的 start_urls 里")
            with _gated_driver(reader):
                seen = -1
                for url in _make_driver(reader.site, reader.profile, request.query).iter_detail_urls():
                    seen += 1
                    if seen < start_i:
                        continue
                    position = seen
                    first = seen == start_i
                    reader.page(url, skip_attachments=start_a if first else 0,
                                record_page=not (first and start_recorded))
            exhausted = True
    except _Resume as stopped:
        stop, resume = stopped.reason, (position, stopped.attachment, stopped.page_recorded)
    except _Stop as stopped:
        stop, resume = stopped.reason, (position, 0, False)
    except HistoryError as exc:
        error, stop = {"code": exc.code, "message": exc.message}, "source_failed"
    except Exception as exc:  # noqa: BLE001 -- 页面抓取失败如实记录，已读原件单独保留
        error = {"code": "source_read_failed", "message": "站点读取失败：" + type(exc).__name__}
        stop = "source_failed"
    next_continuation = None
    if resume and error is None:
        next_continuation = {
            "scope_hash": request.scope_hash(),
            "cursor": json.dumps({"i": resume[0], "a": resume[1], "p": int(resume[2])}),
            "read_count": resume[0],
        }
    return {
        "records": [r for r in reader.records if r is not None],
        "complete": False,
        "upstream_pagination_exhausted": exhausted and error is None,
        "stop_reason": stop,
        "error": error,
        "requests": reader.requests,
        "bytes": reader.bytes,
        "source_total": None,
        "count_drift": None,
        "next_continuation": next_continuation,
        "attachment_coverage": reader.coverage,
    }


def project_capture(source_id: str, item: ArchiveRecord) -> dict:
    """web_capture / attachment 原件的确定性投影。附件正文不在这里，由加工层产出。"""
    try:
        data = json.loads(item.raw_content)
    except ValueError:
        raise HistoryError("invalid_source_fields", "站点原件不是合法 JSON") from None
    role = data.get("role") if isinstance(data, dict) else None
    if role == "web_capture" and data.get("schema_version") == WEB_SCHEMA:
        parsed = data.get("parsed") or {}
        facts = {
            "role": role,
            "parse_method": parsed.get("parse_method"),
            "html_sha256": data.get("html_sha256"),
            "attachment_record_ids": [a.get("record_id") for a in data.get("attachments", [])],
            "extra": parsed.get("extra") or {},
        }
        title, clean_text = parsed.get("title") or "", parsed.get("markdown") or ""
        published = source_time(parsed.get("publish_date") or None, "site_publish_date")
    elif role == "attachment" and data.get("schema_version") == ATT_SCHEMA:
        facts = {
            k: data.get(k)
            for k in ("parent_record_id", "file_name", "file_ext", "sha256", "size", "store_path", "discovered_from")
        }
        facts["role"] = role
        title, clean_text, published = data.get("file_name") or "", "", None
    else:
        raise HistoryError("invalid_source_fields", "站点原件不符合 web_capture / attachment 契约")
    expected = (
        attachment_record_id(data.get("parent_record_id") or "", data.get("url") or "")
        if role == "attachment"
        else data.get("url")
    )
    if expected != item.source_record_id or data.get("site") != source_id:
        raise HistoryError("native_id_mismatch", "站点原件的链接或来源与记录不一致")
    return {
        "record_kind": "document",
        "title": title,
        "source_url": data["url"],
        "clean_text": clean_text,
        "facts": facts,
        "field_locators": {},
        "event_time": None,
        "source_published_at": published.model_dump(mode="json") if published else None,
        "source_updated_at": None,
        "parser_version": "datafabric.legacy_site." + role + ".v1",
    }
