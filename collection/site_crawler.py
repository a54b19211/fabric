"""网站和站点接口的采集插件 site_crawler（唯一采集框架的一部分）。

每个站点的知识只有一份：站点配置 config/sites/*（列表页、选择器、翻页、详情提取器、接口拉取函数）和
biz_rules/extractors/*。抓取、翻页、下载用 core_engine 的 scrapling_client / pagination_driver / router。
落库只有一种：写只追加的 history 版本，不写 documents 表、不把附件文字合并进父文档。
- 详情页 → web_capture 版本（原始 HTML + 提取结果 + 附件清单）；
- 接口记录、表格页的一张表 → site_record 版本（提取结果 + 接口原始字段）；
- 每个附件 → attachment 版本（链接、父记录、sha256、大小、类型、原件路径），正文由加工层
  processing.attachment_parse 按版本产出。附件下载失败只记在本次读取的 attachment_coverage 里。
附件记录身份是“父记录#attachment=附件链接”（attachment_record_id）。

读取方式（FetchRequest.query）：
- "by_url"，source_record_ids=详情页 URL：重抓这些页面及其附件；
- 站点配置里的某个 start_url：按翻页驱动列出详情页（表格页站点则逐张表），读 max_records 条，可续页；
- "records"（接口站点）：按站点的接口拉取函数逐条读，可续读；
- "stored_attachments"，source_record_ids=已存文档 URL：按 documents 库 attachments 表记录的链接
  重新下载附件原件（只读那张表，父页面不重抓）。
每一次真实传输（接口拉取函数里的 httpx/requests/curl 请求及其每一跳重定向、网页抓取、附件下载、浏览器里的
document/xhr/fetch 请求）都在发出处过 collection.transport 的闸门：共享限速、请求/字节/期限预算、超时与 429 的
有界重试，续读重放也计数。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from biz_rules.history_models import ArchiveRecord, HistoryError, source_time
from collection import transport
from collection.catalog import SITE_SOURCES, spec  # noqa: F401  SITE_SOURCES 供调用方使用

WEB_SCHEMA = "datafabric.web_capture.v1"
RECORD_SCHEMA = "datafabric.site_record.v1"
ATT_SCHEMA = "datafabric.attachment.v1"
# v3 6.3: a legacy document rebuilt for this parser by collection.legacy_migration (the old crawler's markdown
# of a page or record); never a web capture or an API response of ours
LEGACY_SCHEMA = "datafabric.site.legacy_capture.v1"
HTML_INLINE_BYTES = 700 * 1024

from config.crawler_settings import SITE_PROFILES  # noqa: E402  站点配置


# ——抓取入口（测试替换这几个名字）——
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

    driver = make_driver(site, profile, start_url)
    driver.delay = 0  # 间隔由共享限速组在每次传输前控制
    return driver


def _store_root() -> Path:
    from config.settings import APP

    return Path(APP.RAW_DIR)


@lru_cache(maxsize=1)
def _documents_engine():
    from sqlalchemy import create_engine

    from config.settings import APP

    return create_engine(APP.DB_URL, pool_pre_ping=True, connect_args={"options": "-c default_transaction_read_only=on"})


def _stored_attachment_links(site, parent_url):
    """documents 库 attachments 表里这个父文档的附件链接（只读、有界）。文档 uuid = uuid5(URL)。"""
    from sqlalchemy import text

    from core_engine.scrapling_client import make_uuid

    with _documents_engine().begin() as conn:
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


_Stop = transport.Stop  # 预算用完（BaseException，拉取函数里的 except Exception 吞不掉）


class _Resume(_Stop):
    """预算在某一父项的附件中途用完：attachment 是下一个要读的附件序号；page_recorded 表示这一页的
    页面版本已经保存，续读时只为拿附件链接和会话重取页面，不再占版本名额（否则 max_records=1
    时每轮都被父页面占满，永远读不到附件）。"""

    def __init__(self, reason, attachment, page_recorded=False):
        super().__init__(reason)
        self.attachment = attachment
        self.page_recorded = page_recorded


class _Reader:
    """一次读取：版本、附件覆盖和续读位置。预算与计量是本次读取共用的 transport.Budget——版本数 ≤ max_records
    （父页面和附件都算），请求、字节、截止时间在每次真实传输的发出处执行；单个附件 ≤ DATAFABRIC_ATTACHMENT_MAX_BYTES。
    任何一项用完就停：已读到的保留，stop_reason 写明是哪一项，给续读位置，不声称读到底。"""

    def __init__(self, request, budget):
        self.request = request
        self.budget = budget
        self.site = request.source_id
        self.profile = SITE_PROFILES[self.site]
        self.spec = spec(self.site)
        self.records: list[ArchiveRecord | None] = []
        # 本次读取的附件覆盖情况（stored / download_failed / too_large / not_attempted ...）。放在读取结果里，
        # 不放进页面原件：原件只放来源给出的内容，否则同一页面每重试一次就因状态不同多一个版本。
        self.coverage: list[dict] = []

    def before_record(self):
        self.budget.need_record()

    def add(self, record, at=None):
        """一个版本占一个名额（at：填回先占的位置）。"""
        if at is None:
            self.budget.take_record()
            self.records.append(record)
        else:
            self.records[at] = record

    def attachment(self, link, parent_id, cookies, discovered):
        """下载一个附件；返回父页面附件清单里的一项，成功时同时加一条附件版本。预算用完抛 _Stop。"""
        url = (link or {}).get("url") or ""
        record_id = attachment_record_id(parent_id, url)
        entry = {"record_id": record_id, "url": url, "text": (link or {}).get("text", ""), "status": "stored"}
        self.before_record()
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
        entry.update(sha256=sha, size=size)
        self.add(
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

    def site_record(self, parsed, raw, skip_attachments=0, record_page=True):
        """接口记录或表格页的一张表 → 一条 site_record 版本；结果里列出的附件和详情页一样各成版本。
        记录身份是站点给的稳定编号（接口：uuid5(站点:记录键)，与 documents 库的文档 uuid 同一算法）。"""
        record_id = parsed.get("uuid") or ""
        if not record_id:
            raise HistoryError("native_id_missing", "站点记录缺少稳定编号")
        if record_page:
            self.before_record()
        links = [link for link in (parsed.get("attachment_links") or []) if link and link.get("url")]
        if record_page:
            self.add(_record(record_id, {
                "schema_version": RECORD_SCHEMA,
                "role": "site_record",
                "site": self.site,
                "url": parsed.get("source_url") or "",
                "record_id": record_id,
                "parsed": {k: parsed.get(k) for k in (
                    "title", "publish_date", "markdown", "parse_method", "structured_data", "extra")},
                # 接口原始字段；拉取函数已直接给出解析结果的站点没有原始字段（如实为空）
                "raw": raw,
                "attachments": [
                    {"record_id": attachment_record_id(record_id, link["url"]), "url": link["url"],
                     "text": link.get("text", "")}
                    for link in links
                ],
            }))
        for position, link in enumerate(links):
            if position < skip_attachments:
                continue
            try:
                self.attachment(link, record_id, None, "site_record")
            except _Stop as stop:
                for later in links[position:]:
                    self.coverage.append({"parent_record_id": record_id, "url": later["url"],
                                          "status": "not_attempted",
                                          "record_id": attachment_record_id(record_id, later["url"])})
                raise _Resume(stop.reason, position, page_recorded=True) from None

    def page(self, url, skip_attachments=0, record_page=True):
        """读一个详情页和它的附件。skip_attachments：续读时这一页前面已读过的附件数；record_page=False：
        这一页的页面版本上一轮已保存，这次只为拿附件链接和会话重取，不再占版本名额。
        预算在附件中途用完时，父页面照常保存，抛 _Resume（续读位置指向下一个附件）。"""
        cfg = self.profile
        if record_page:
            self.before_record()
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
            self.add(None)  # 占位：父页面排在它的附件前面
        cookies = page_data.get("cookies") or []
        links = [link for link in (parsed.get("attachment_links") or []) if link and link.get("url")]
        # 原件里的附件清单只有来源给出的链接和文字；每个附件这次读没读到记在 coverage
        capture["attachments"] = [
            {"record_id": attachment_record_id(url, link["url"]), "url": link["url"], "text": link.get("text", "")}
            for link in links
        ]
        if record_page:
            self.add(_record(url, capture), at=index)
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


def _site_results(reader, query):
    """接口站点（query="records"）逐条给出（解析结果, 接口原始字段）；表格页站点（query=start_url）逐张表给出
    （解析结果, None）。续读时从头重放到续读位置（拉取函数没有可恢复的游标）。"""
    profile = reader.profile
    if profile.get("source_type") == "api":
        if query != "records":
            raise HistoryError("unknown_query", "接口站点只支持 query=records")
        from core_engine.api_router import to_result

        for raw in profile["api_fetcher"](profile.get("api", {})):
            parsed, kept = to_result(reader.site, profile, raw)
            yield parsed, kept
        return
    if query not in profile.get("start_urls", []):
        raise HistoryError("unknown_start_url", "列表入口不在该站点配置的 start_urls 里")
    for batch in _make_driver(reader.site, profile, query).iter_table_data():
        for parsed in batch:
            yield parsed, None


def _cursor(request):
    if not request.continuation:
        return 0, 0, False
    try:
        value = json.loads(request.continuation.cursor)
        return int(value["i"]), int(value["a"]), bool(value.get("p"))
    except (ValueError, KeyError, TypeError):
        raise HistoryError("invalid_cursor", "续读位置无效") from None


def read_site(request) -> dict:
    """site_crawler 插件入口：这次读取的全部真实传输都过同一个闸门和预算。"""
    with transport.reading(request) as budget:
        return _read(request, _Reader(request, budget))


def _read(request, reader) -> dict:
    """site_crawler 插件入口；返回值形状与 read_public 其他插件相同。
    续读位置 cursor = {"i": 第几个父项（详情页 / 已存父文档 / 列表里第几个详情页或记录）, "a": 该父项下第几个附件}。"""
    error, stop, exhausted, resume = None, "upstream_exhausted", False, None
    position = 0
    try:
        start_i, start_a, start_recorded = _cursor(request)
        position = start_i
        if request.query in {"by_url", "stored_attachments"}:
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
                for k, link in enumerate(_stored_attachment_links(reader.site, url)):
                    if k < skip:
                        continue
                    try:
                        reader.attachment(link, url, None, "stored_attachments_table")
                    except _Stop as stop_here:
                        raise _Resume(stop_here.reason, k, page_recorded=False) from None
            exhausted = True
        elif reader.profile.get("source_type") == "api" or reader.profile.get("source_type") == "table_page":
            for position, (parsed, raw) in enumerate(_site_results(reader, request.query)):
                if position < start_i:
                    continue
                first = position == start_i
                reader.site_record(parsed, raw, skip_attachments=start_a if first else 0,
                                   record_page=not (first and start_recorded))
            exhausted = True
        else:
            if request.query not in reader.profile.get("start_urls", []):
                raise HistoryError("unknown_start_url", "列表入口不在该站点配置的 start_urls 里")
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
        if reader.budget.stopped:  # 浏览器里的请求因预算被中止，驱动随后报的错不是来源失败
            stop, resume = reader.budget.stopped, (position, 0, False)
        else:
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
        "requests": reader.budget.requests,
        "bytes": reader.budget.bytes,
        "subrequests": reader.budget.subrequests,
        "source_total": None,
        "count_drift": None,
        "next_continuation": next_continuation,
        "attachment_coverage": reader.coverage,
    }


def project_capture(source_id: str, item: ArchiveRecord) -> dict:
    """web_capture / site_record / attachment 原件的确定性投影。附件正文不在这里，由加工层产出。"""
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
    elif role == "site_record" and data.get("schema_version") == RECORD_SCHEMA:
        parsed = data.get("parsed") or {}
        facts = {
            "role": role,
            "parse_method": parsed.get("parse_method"),
            "structured": parsed.get("structured_data") or {},
            "extra": parsed.get("extra") or {},
            "attachment_record_ids": [a.get("record_id") for a in data.get("attachments", [])],
            "source_fields_kept": data.get("raw") is not None,
        }
        title, clean_text = parsed.get("title") or "", parsed.get("markdown") or ""
        published = source_time(parsed.get("publish_date") or None, "site_publish_date")
    elif role == "legacy_capture" and data.get("schema_version") == LEGACY_SCHEMA:
        parsed = data.get("parsed") or {}
        facts = {"role": role, "capture_kind": data.get("kind"), "parse_method": "legacy_documents_markdown"}
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
        raise HistoryError("invalid_source_fields",
                           "站点原件不符合 web_capture / site_record / attachment / legacy_capture 契约")
    if role == "attachment":
        expected = attachment_record_id(data.get("parent_record_id") or "", data.get("url") or "")
    elif role in ("site_record", "legacy_capture"):
        expected = data.get("record_id")
    else:
        expected = data.get("url")
    if expected != item.source_record_id or data.get("site") != source_id:
        raise HistoryError("native_id_mismatch", "站点原件的链接或来源与记录不一致")
    return {
        "record_kind": "document",
        "title": title,
        "source_url": data.get("url") or None,
        "clean_text": clean_text,
        "facts": facts,
        "field_locators": {},
        "event_time": None,
        "source_published_at": published.model_dump(mode="json") if published else None,
        "source_updated_at": None,
        "parser_version": "datafabric.site." + role + ".v1",
    }
