"""网站和站点接口的采集插件 site_crawler（唯一采集框架的一部分）。

每个站点的知识只有一份：站点配置 fetch/sites/*（列表页、选择器、翻页、详情提取器、接口拉取函数）和
fetch/extractors/*。抓取、翻页、下载用 fetch/web_client.py、pagination.py、page_parser.py。
落库只有一种：写只追加的资料库版本，不写 documents 表、不把附件文字合并进父文档。
- 详情页 → web_capture 版本（原始 HTML + 提取结果 + 附件清单）；
- 接口记录、表格页的一张表 → site_record 版本（提取结果 + 接口原始字段）；
- 每个附件 → attachment 版本（链接、父记录、sha256、大小、类型、原件路径），正文由加工层
  clean.attachment_parse 按版本产出。附件下载失败只记在本次读取的 attachment_coverage 里。
附件记录身份是“父记录#attachment=附件链接”（attachment_record_id）。下载链接每次都不同的（ChiCTR 的链接带一次性令牌），
抽取器给附件一个固定的 key（页面上的字段名），身份改用“父记录#attachment=key”，带令牌的链接只在下载那一刻用，
不存进原件（否则每抓一次就多一个附件、多一个版本）。
一条记录读不到（拉取函数产出 ItemUnreadable：如 FiercePharma、Roche 的一篇详情页读了两次都不行；列表站点的一个详情页
读不出来也一样）：记在本次读取的 unread_items（网址 + 原因），不存版本，接着读下一条；读到底时运行标“部分完成”，
原因“有 N 条读不到”。连续 SITE_DOWN_AFTER 条都读不出来，不是某几页的问题，是站点出了故障：不再往下读，整次读取失败。
没存的下一轮从头读会再碰到：固定刷新里一个周期有条目读不到（连续几轮都读不到的死链除外），下一个周期不用“遇到已存的就停”，
从头整份读（fetch.source_refresh 的 read_all）。
列表一条都没解析到（列表页改版、接口改格式的样子）不算读完，标成失败（list_empty）；“解析到了但全是已存的”是正常读到旧的了。
增量读取（FetchRequest.known_stop）：每读完一条父记录（页面或接口记录）问一次 transport.Budget.known，已存且没变的父记录
不再下载它的附件（上一轮存过了）；连续够 known_stop 条就以 reached_known 停，不给续读位置。
分组读取（站点配置里 "by_group": True：港交所一家公司一组，NCCN 一份指南一组）：拉取函数在每一组开头给一个
GroupStart(第几组)，并从续读位置所在的那一组读起，不重放前面的组。续读位置多一个 "g"（第几组），"i" 是组内第几条。
“连续读到已存的就停”按组算：这一组读到旧的了，丢掉它剩下的条目，接着读下一组，一个周期读完全部的组。

读取方式（FetchRequest.query）：
- "by_url"，source_record_ids=详情页 URL：重抓这些页面及其附件；
- 站点配置里的某个 start_url：按翻页驱动列出详情页（表格页站点则逐张表），读 max_records 条，可续页；
- "records"（接口站点）：按站点的接口拉取函数逐条读，可续读；
- "stored_attachments"，source_record_ids=已存文档 URL：按 documents 库 attachments 表记录的链接
  重新下载附件原件（只读那张表，父页面不重抓）。
打开页面、下载附件、接口请求之前都等站点的限速（fetch.transport.wait / get；接口请求和附件下载的超时与 429 有界重试
也在 get 里）。读几条（max_records，页面和附件都算）、截止时间、“连续读到已存的就停”在这里的循环里数（transport.Budget）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlparse

from loguru import logger

from fetch import transport
from fetch.catalog import SITE_SOURCES, spec  # noqa: F401  SITE_SOURCES 供调用方使用
from fetch.extractors.text_tools import remove_extra_whitespace
from fetch.page_parser import route_webpage
from fetch.pagination import make_driver
from fetch.web_client import download_pdf, fetch_page
from fetch.windows import GROUP_LIMIT
from store.record_models import ArchiveRecord, FabricError, GroupStart, ItemUnreadable, make_uuid, source_time

WEB_SCHEMA = "datafabric.web_capture.v1"
RECORD_SCHEMA = "datafabric.site_record.v1"
ATT_SCHEMA = "datafabric.attachment.v1"
# v3 6.3：旧文档由 fetch.legacy_migration 重建给这个解析器用（旧爬虫存的页面或记录的 Markdown）；
# 不是我们自己的网页原件，也不是接口响应
LEGACY_SCHEMA = "datafabric.site.legacy_capture.v1"
HTML_INLINE_BYTES = 700 * 1024
LIST_EMPTY = "列表页一条都没解析到，可能改版"  # 列表里一条都没有：不是“没有新内容”（那是解析到了、但全是已存的）
SITE_DOWN_AFTER = 5  # 连续这么多条读不出来，就当站点出了故障，整次读取失败

from fetch.site_profiles import SITE_PROFILES  # noqa: E402  站点配置


def site_proxy(site):
    """这个站点的代理：站点配置里 proxy_env 写的环境变量（nccn 是 DATAFABRIC_PROXY_NCCN，如 http://127.0.0.1:7890）。
    设了，这个站点的页面渲染和附件下载都经它；站点没有 proxy_env 或变量没设，返回 None，直连。只管这一个站点，不设全局代理。"""
    name = SITE_PROFILES.get(site, {}).get("proxy_env")
    return (os.environ.get(name) or None) if name else None


def _store_root() -> Path:
    from settings import APP

    return Path(APP.RAW_DIR)


@lru_cache(maxsize=1)
def _documents_engine():
    from sqlalchemy import create_engine

    from settings import APP

    return create_engine(APP.DB_URL, pool_pre_ping=True, connect_args={"options": "-c default_transaction_read_only=on"})


def _stored_attachment_links(site, parent_url):
    """documents 库 attachments 表里这个父文档的附件链接（只读、有界）。文档 uuid = uuid5(URL)。"""
    from sqlalchemy import text

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


@lru_cache(maxsize=1)
def _fabric_reader():
    from sqlalchemy import create_engine

    url = os.environ.get("DATAFABRIC_DATABASE_URL", "")
    return create_engine(url, pool_size=1, max_overflow=1, pool_pre_ping=True) if url else None


def _article_record(site, key):
    """这篇文章（编号 key）在库里的记录号：最早存下它的那个页面地址（同时存过几个地址的，取最早的，再按地址排）。
    没存过、或没配资料库（单元测试）返回 None。只读、有界。"""
    engine = _fabric_reader()
    if engine is None:
        return None
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='10s'"))
        return conn.execute(text(
            "SELECT source_record_id FROM fabric_versions WHERE source_id=:s AND source_record_id LIKE :p "
            "AND position('#' in source_record_id)=0 GROUP BY source_record_id "
            "ORDER BY min(first_seen_at), source_record_id LIMIT 1"), {"s": site, "p": "%/" + key + ".html"}).scalar()


def _duplicate_article(site, url, key, seen):
    """一篇一条记录（审计 F4）：同一篇文章（编号相同）挂在几个栏目、几个地址下，记录号是最早存下它的那个地址，
    别的地址都跳过（存量里已经两个地址各存一份的，也只再读最早的那个）。这次读取里已经见过这个编号也跳过。"""
    if key in seen:
        return True
    seen.add(key)
    canonical = _article_record(site, key)
    return canonical is not None and canonical != url


def attachment_record_id(parent_id: str, url: str) -> str:
    """附件记录身份 = 父页面 + 附件链接。同一文件挂在两个页面下（NMPA 同一公告在两个栏目各有地址）是两条
    稳定记录，不会来回换版本；同一份文件的原件按 sha256 只存一份，解析结果也按 sha256 复用。"""
    rid = parent_id + "#attachment=" + url
    if len(rid) > 400:
        rid = parent_id + "#attachment-sha256=" + hashlib.sha256(url.encode()).hexdigest()
    return rid


def _links(parsed):
    """解析结果里的附件链接，整理成 {"url", "text"}：提取器把名称叫 "name"，已存附件表叫 "text"。每次访问都不同的链接
    （ChiCTR 的带一次性令牌）还有一个 "key"，是这个附件固定的名字（页面上的字段名）：见 _name。"""
    links = []
    for link in parsed.get("attachment_links") or []:
        if link and link.get("url"):
            links.append({"url": link["url"], "text": link.get("text") or link.get("name") or "",
                          **({"key": link["key"]} if link.get("key") else {})})
    return links


def _name(link):
    """附件在父项下的名字：它的链接；链接每次访问都变的，用它的 key。"""
    return link.get("key") or link["url"]


def _kept_url(link):
    """原件里保留的链接。带 key 的链接只用来下载，不保留：它的令牌会让同一个页面每读一次都像是新版本。"""
    return "" if link.get("key") else link["url"]


def _listed(parent_id, link):
    """父项的原件里怎样列出一个附件链接。"""
    return {"record_id": attachment_record_id(parent_id, _name(link)), "url": _kept_url(link),
            "text": link.get("text", "")}


def _sha256_file(path: str) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _on_site(site, url) -> bool:
    """url 的主机是站点主机（站点配置里 url_pattern 的主机部分，如 "fda.gov/news-events/..." 取 "fda.gov"）或它的子域。"""
    host = (SITE_PROFILES[site].get("url_pattern") or "").split("/")[0].lower()
    name = (urlparse(url).hostname or "").lower()
    return bool(name) and (name == host or name.endswith("." + host))


def _record(record_id, data) -> ArchiveRecord:
    return ArchiveRecord(
        source_record_id=record_id,
        record_kind="document",
        raw_content=json.dumps(data, ensure_ascii=False, sort_keys=True),
    )


class _Resume(transport.Stop):
    """预算在某一父项的附件中途用完：attachment 是下一个要读的附件序号；page_recorded 表示这一页的
    页面版本已经保存，续读时只为拿附件链接和会话重取页面，不再占版本名额（否则 max_records=1
    时每轮都被父页面占满，永远读不到附件）。"""

    def __init__(self, reason, attachment, page_recorded=False):
        super().__init__(reason)
        self.attachment = attachment
        self.page_recorded = page_recorded


class _Reader:
    """一次读取：版本、附件覆盖和续读位置。额度是 transport.Budget——版本数 ≤ max_records（父页面和附件都算）、
    截止时间，每存一条、每下一个附件之前查；单个附件 ≤ DATAFABRIC_ATTACHMENT_MAX_BYTES。
    任何一项用完就停：已读到的保留，stop_reason 写明是哪一项，给续读位置，不声称读到底。"""

    def __init__(self, request):
        self.request = request
        self.budget = transport.Budget(request)
        self.site = request.source_id
        self.profile = SITE_PROFILES[self.site]
        self.spec = spec(self.site)
        self.records: list[ArchiveRecord | None] = []
        # 本次读取的附件覆盖情况（stored / download_failed / too_large / not_a_file / not_attempted ...）。放在读取结果里，
        # 不放进页面原件：原件只放来源给出的内容，否则同一页面每重试一次就因状态不同多一个版本。
        self.coverage: list[dict] = []
        # 本次读取里读不到的条目（网址 + 原因），同样只记在读取结果里，不存版本
        self.unread: list[dict] = []
        self.unread_in_a_row = 0  # 连续读不到的条数；读到一条就清零
        # 列表里指向别的网站的条目（NMPA 栏目里转链到药审中心、医保局、市场监管总局的文件）：不读不存，记在读取结果里。
        # 用本站的提取器读别站页面读不出正文，以前存下来的是通用兜底抽出的整页文字
        self.skipped: list[dict] = []
        self.position = 0  # 读到第几个父项了（分组读取：这一组里第几条）；停下时的续读位置指向它
        self.group = 0  # 分组读取：读到第几组了；不分组的站点一直是 0

    def add(self, record, at=None):
        """一个版本占一个名额（at：填回先占的位置）。"""
        if at is None:
            self.budget.take_record()
            self.records.append(record)
        else:
            self.records[at] = record

    def skip(self, unread):
        """拉取函数读不到一条记录（ItemUnreadable）：记下网址和原因，不存版本，读取接着往下走。"""
        self.budget.check()  # 这期间时间用完了：那是停在这一条（续读从这里接），不是这一条读不到
        self.budget.known(None)  # 没存的不算“已存”：连续数清零，“遇到已存的就停”跳不过它
        self.unread.append({"url": unread.url, "reason": unread.reason})
        logger.warning("站点 {} 有一条读不到：{} | {}", self.site, unread.reason, unread.url)
        self.unread_in_a_row += 1
        if self.unread_in_a_row >= SITE_DOWN_AFTER:
            raise FabricError("site_down", f"连续 {SITE_DOWN_AFTER} 条读不出来，站点可能出了故障；最近一条 {unread.url}：{unread.reason}")

    def attachment(self, link, parent_id, cookies, discovered, parent=None):
        """下载一个附件；返回父页面附件清单里的一项，成功时同时加一条附件版本。额度用完抛 transport.Stop。
        parent：所属页面的标题、发布日期、文号（_parent_context），写进附件原件：附件记录继承所属公告的日期，
        进知识库时开头写所属公告的标题和文号。"""
        url = (link or {}).get("url") or ""
        key = (link or {}).get("key") or ""
        record_id = attachment_record_id(parent_id, _name(link))
        entry = {"record_id": record_id, "url": _kept_url(link), "text": (link or {}).get("text", ""),
                 "status": "stored"}
        self.budget.need_record()
        try:
            # 带 key 的链接（每次都不同）：原件落盘的路径也用附件记录号，同一个文件每次落在同一处
            info = download_pdf(url, parent_uuid=parent_id, source_site=self.site, cookies=cookies,
                                store_as=record_id if key else "", proxy=site_proxy(self.site))
            sha, size = _sha256_file(info["local_path"])
        except ValueError as exc:
            status = {"response_too_large": "too_large", "html_page_not_a_file": "not_a_file"}.get(
                str(exc), "download_failed")
            entry.update(status=status, error=str(exc)[:80])
            self.coverage.append({"parent_record_id": parent_id, **entry})
            return entry
        except Exception as exc:  # noqa: BLE001 -- 附件失败是覆盖缺口，不让父页面失败
            entry.update(status="download_failed", error=type(exc).__name__)
            self.coverage.append({"parent_record_id": parent_id, **entry})
            return entry
        entry.update(sha256=sha, size=size)
        # 文件名：页面上的文字；没有就用网址末段。带 key 的链接网址里没有文件名，用站点在响应头里给的名字
        name = entry["text"] or (key and info.get("file_name")) or key or Path(urlparse(url).path).name
        self.add(
            _record(
                record_id,
                {
                    "schema_version": ATT_SCHEMA,
                    "role": "attachment",
                    "site": self.site,
                    "url": entry["url"],
                    **({"key": key} if key else {}),
                    "parent_record_id": parent_id,
                    "file_name": name,
                    "file_ext": (info.get("file_ext") or "").lower(),
                    "sha256": sha,
                    "size": size,
                    "store_path": info["local_path"],
                    "discovered_from": discovered,
                    "status": "stored",
                    **({"parent": parent} if parent else {}),
                },
            )
        )
        self.coverage.append({"parent_record_id": parent_id, **entry})
        return entry

    def attachments(self, links, parent_id, cookies, discovered, skip, parent=None):
        """下载一个父项的附件（前 skip 个上一轮读过了，跳过）。预算在中途用完：后面的记成 not_attempted，
        抛 _Resume，续读位置指向下一个要读的附件。"""
        for position, link in enumerate(links):
            if position < skip:
                continue
            try:
                self.attachment(link, parent_id, cookies, discovered, parent)
            except transport.Stop as stop:
                for later in links[position:]:
                    self.coverage.append({"parent_record_id": parent_id, "url": _kept_url(later),
                                          "status": "not_attempted",
                                          "record_id": attachment_record_id(parent_id, _name(later))})
                raise _Resume(stop.reason, position, page_recorded=True) from None

    def site_record(self, parsed, raw, skip_attachments=0, record_page=True):
        """接口记录或表格页的一张表 → 一条 site_record 版本；结果里列出的附件和详情页一样各成版本。
        记录身份是站点给的稳定编号（接口：uuid5(站点:记录键)，与 documents 库的文档 uuid 同一算法）。"""
        record_id = parsed.get("uuid") or ""
        if not record_id:
            raise FabricError("native_id_missing", "站点记录缺少稳定编号")
        if record_page:
            self.budget.need_record()
        links = _links(parsed)
        if record_page:
            record = _record(record_id, {
                "schema_version": RECORD_SCHEMA,
                "role": "site_record",
                "site": self.site,
                "url": parsed.get("source_url") or "",
                "record_id": record_id,
                "parsed": {k: parsed.get(k) for k in (
                    "title", "publish_date", "markdown", "parse_method", "structured_data", "extra")},
                # 接口原始字段；拉取函数已直接给出解析结果的站点没有原始字段（如实为空）
                "raw": raw,
                "attachments": [_listed(record_id, link) for link in links],
            })
            self.add(record)
            self.unread_in_a_row = 0
            if self.budget.known(record):  # 增量读取：已存且没变，附件也是上一轮存的，不再下载
                return
        self.attachments(links, record_id, None, "site_record", skip_attachments,
                         _parent_context(self.profile, parsed))

    def page(self, url, skip_attachments=0, record_page=True):
        """读一个详情页和它的附件。skip_attachments：续读时这一页前面已读过的附件数；record_page=False：
        这一页的页面版本上一轮已保存，这次只为拿附件链接和会话重取，不再占版本名额。
        预算在附件中途用完时，父页面照常保存，抛 _Resume（续读位置指向下一个附件）。"""
        cfg = self.profile
        if record_page:
            self.budget.need_record()
        page_data = fetch_page(
            url,
            wait_selector=cfg.get("detail_wait_selector"),
            disable_resources=cfg.get("disable_resources", True),
            solve_cloudflare=cfg.get("solve_cloudflare", False),
            use_simple_fetch=cfg.get("use_simple_fetch", False),
        )
        parsed = route_webpage(page_data, site_key=self.site)
        subpages = self.continuation(url, page_data["page"], parsed)
        if cfg.get("markdown_finish") and parsed.get("markdown"):  # 站点对整篇正文的收尾（NMPA：小标题统一定级）
            parsed["markdown"] = cfg["markdown_finish"](parsed["markdown"])
        html = getattr(page_data["page"], "html_content", "") or ""
        raw = html.encode("utf-8")
        html_sha = hashlib.sha256(raw).hexdigest()
        capture = {
            "schema_version": WEB_SCHEMA,
            "role": "web_capture",
            "site": self.site,
            "url": url,
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
        if subpages:
            capture["continuation_pages"] = subpages
        index = len(self.records)
        if record_page:
            self.add(None)  # 占位：父页面排在它的附件前面
        cookies = page_data.get("cookies") or []
        links = _links(parsed)
        # 原件里的附件清单只有来源给出的链接和文字；每个附件这次读没读到记在 coverage
        capture["attachments"] = [_listed(url, link) for link in links]
        if record_page:
            record = _record(url, capture)
            self.add(record, at=index)
            self.unread_in_a_row = 0
            if self.budget.known(record):  # 增量读取：已存且没变，附件也是上一轮存的，不再下载
                return
        self.attachments(links, url, cookies, "detail_page", skip_attachments, _parent_context(cfg, parsed))

    def continuation(self, url, page, parsed):
        """多页文章（站点配置有 continuation_links / continuation_extractor，如 NMPA“共3页”）：逐页抓子页，
        正文接在第 1 页后面（空一行），子页上的附件并进附件清单；返回各子页的原件 [{url, html 或 html_store_path}]，
        存进第 1 页的原件。子页读不出来，整篇算读不到（抛出），不存只有第 1 页的半篇。"""
        extract, find = self.profile.get("continuation_extractor"), self.profile.get("continuation_links")
        hrefs = find(page) if extract and find else []
        if not hrefs:
            return []
        cfg, pages = self.profile, []
        known = {link.get("url") for link in parsed.get("attachment_links") or []}
        for href in hrefs:
            sub_url = urljoin(url, href)
            if not _on_site(self.site, sub_url):
                continue
            sub = fetch_page(sub_url, wait_selector=cfg.get("detail_wait_selector"),
                             disable_resources=cfg.get("disable_resources", True),
                             solve_cloudflare=cfg.get("solve_cloudflare", False),
                             use_simple_fetch=cfg.get("use_simple_fetch", False))
            got = extract(sub["page"])
            body = remove_extra_whitespace(got.get("body_text") or "")
            if body:
                parsed["markdown"] = ((parsed.get("markdown") or "# " + (parsed.get("title") or "")) + "\n\n" + body)
            for link in got.get("attachment_links") or []:
                if link.get("url") not in known:
                    known.add(link.get("url"))
                    parsed.setdefault("attachment_links", []).append(link)
            raw = (getattr(sub["page"], "html_content", "") or "").encode("utf-8")
            entry = {"url": sub_url}
            if len(raw) <= HTML_INLINE_BYTES:
                entry["html"] = raw.decode("utf-8")
            else:
                path = _store_root() / self.site / "html" / (hashlib.sha256(raw).hexdigest() + ".html")
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_bytes(raw)
                entry["html_store_path"] = str(path)
            pages.append(entry)
        if pages:
            parsed["extra"] = {**(parsed.get("extra") or {}), "page_count": 1 + len(pages)}
        return pages


def _parent_context(profile, parsed):
    """附件继承的所属页面信息（站点配置 attachments_inherit_parent 打开的站点）：标题、发布日期、文号。"""
    if not profile.get("attachments_inherit_parent"):
        return None
    extra = parsed.get("extra") or {}
    context = {"title": parsed.get("title") or "", "publish_date": parsed.get("publish_date") or "",
               "doc_number": extra.get("doc_number") or ""}
    return {k: v for k, v in context.items() if v} or None


def _site_results(reader, query, resume_at=0):
    """接口站点（query="records"）逐条给出（解析结果, 接口原始字段）；表格页站点（query=start_url）逐张表给出
    （解析结果, None）。续读时从头重放到续读位置（拉取函数没有可恢复的游标）。"""
    profile = reader.profile
    if profile.get("source_type") == "api":
        if query != "records":
            raise FabricError("unknown_query", "接口站点只支持 query=records")
        from fetch.api_fields import to_result

        fetch = profile["api_fetcher"]
        api = {**profile.get("api", {}), "proxy": site_proxy(reader.site)}  # 拉取函数自己开浏览器时用它（nccn）
        # 续读时前 resume_at 条是重放、读取方会丢掉：拉取函数可以只给占位、不读详情页（roche）
        api["resume_at"] = 0 if profile.get("by_group") else resume_at  # 分组读取不重放
        for raw in fetch(api, reader.group) if profile.get("by_group") else fetch(api):
            if isinstance(raw, ItemUnreadable | GroupStart):  # 读不到这一条、接着往下读了 / 新的一组：原样传下去
                yield raw, None
                continue
            parsed, kept = to_result(reader.site, profile, raw)
            yield parsed, kept
        return
    if query not in profile.get("start_urls", []):
        raise FabricError("unknown_start_url", "列表入口不在该站点配置的 start_urls 里")
    with closing(make_driver(reader.site, profile, query).iter_table_data()) as pages:  # 读满不再要，浏览器就收工
        for batch in pages:
            for parsed in batch:
                yield parsed, None


def _cursor(request):
    """续读位置 → (第几个父项, 第几个附件, 页面版本是否已保存, 第几组)。"""
    if not request.continuation:
        return 0, 0, False, 0
    try:
        value = json.loads(request.continuation.cursor)
        return int(value["i"]), int(value["a"]), bool(value.get("p")), int(value.get("g", 0))
    except (ValueError, KeyError, TypeError):
        raise FabricError("invalid_cursor", "续读位置无效") from None


def _read_named(request, reader, start):
    """query=by_url / stored_attachments：父项就是请求里的记录号，按顺序读。"""
    start_i, start_a, start_recorded = start[:3]
    for url in request.source_record_ids:
        if not _on_site(reader.site, url):
            raise FabricError("url_outside_site", "链接不属于该来源站点")
    for position in range(start_i, len(request.source_record_ids)):
        reader.position = position
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
            except transport.Stop as stop_here:
                raise _Resume(stop_here.reason, k, page_recorded=False) from None


def _read_site_records(request, reader, start):
    """接口站点和表格页站点：父项是站点给出的各条结果，续读时从头重放。分组读取的站点（by_group）不重放：拉取函数从续读位置
    所在的那一组读起，组内前 start_i 条上一段读过了，丢掉；这一组读到旧的了（或读够 GROUP_LIMIT 条）就丢掉它剩下的，到下一组。"""
    start_i, start_a, start_recorded, group = start
    by_group = reader.profile.get("by_group")
    position, skipping, yielded = -1, False, False  # position：这一组（不分组的站点是整个结果）里第几条
    with closing(_site_results(reader, request.query, start_i)) as results:
        for parsed, raw in results:
            if isinstance(parsed, GroupStart):
                reader.group, position, skipping = parsed.index, -1, False
                reader.position = start_i if parsed.index == group else 0
                reader.budget.known_run = 0  # “连续几条已存”按组数
                reader.budget.check()  # 到点了就停在这一组的开头，续读从这一组读起
                continue
            yielded, position = True, position + 1
            if skipping or (reader.group == group and position < start_i):
                continue  # 丢掉的：这一组已经够了；或前面的是重放：续读位置只往前走，重放途中停下也不倒退
            reader.position = position
            try:
                if isinstance(parsed, ItemUnreadable):  # 读不到：不存版本，记下，接着读下一条
                    reader.skip(parsed)
                else:
                    first = reader.group == group and position == start_i
                    reader.site_record(parsed, raw, skip_attachments=start_a if first else 0,
                                       record_page=not (first and start_recorded))
            except transport.Stop as stop:
                if not (by_group and stop.reason == transport.KNOWN):
                    raise
                skipping = True  # 这一组读到旧的了
            # 拉取函数在循环来要下一条时才去读它：停在那里，就从那一条接着读
            reader.position = position + 1
            skipping = skipping or bool(by_group and request.known_stop and position + 1 >= GROUP_LIMIT)
    if not (yielded or start_i or group):
        raise FabricError("list_empty", LIST_EMPTY)


def _read_detail_pages(request, reader, start):
    """列表网页站点：父项是列表入口通向的各个详情页。"""
    start_i, start_a, start_recorded = start[:3]
    if request.query not in reader.profile.get("start_urls", []):
        raise FabricError("unknown_start_url", "列表入口不在该站点配置的 start_urls 里")
    seen, articles = -1, set()
    column = (reader.profile.get("list_filters") or {}).get(request.query)
    article_key = reader.profile.get("article_key")
    with closing(make_driver(reader.site, reader.profile, request.query).iter_detail_urls()) as urls:
        for url in urls:  # 读满不再要，浏览器就收工，不再翻下一页
            seen += 1
            if seen < start_i:
                if article_key and article_key(url):
                    articles.add(article_key(url))
                continue
            key = article_key(url) if article_key else None
            reason = ("off_site_link" if not _on_site(reader.site, url)
                      else "outside_column" if column and not re.search(column, url)
                      else "duplicate_article" if key and _duplicate_article(reader.site, url, key, articles)
                      else None)
            reader.position = seen
            first = seen == start_i
            if reason:
                # 别站的链接；栏目里不采的子栏（器械、化妆品、图解）；同一篇文章在列表里出现第二次（一篇一条记录）
                reader.skipped.append({"url": url, "reason": reason})
                reader.position = seen + 1
                continue
            try:
                reader.page(url, skip_attachments=start_a if first else 0,
                            record_page=not (first and start_recorded))
            except Exception as exc:  # noqa: BLE001 -- 这一页读不出来：和接口站点的 ItemUnreadable 一样记下，接着读下一页
                reader.skip(ItemUnreadable(url, _why(exc)))
            reader.position = seen + 1  # 这一项读完了：下一页列表要在这之后才取，停在那里就从下一项接
    if seen < 0:
        raise FabricError("list_empty", LIST_EMPTY)


def _why(exc) -> str:
    """失败原因：异常类名加信息里的关键一句（第一行，最长 200 字）。网址里的查询串和 key=值 这类写法可能带密钥，
    先去掉。"""
    line = next((part.strip() for part in str(exc).splitlines() if part.strip()), "")
    line = re.sub(r"(https?://[^\s?#]+)[?#]\S*", r"\1", line)
    line = re.sub(r"(?i)\b(\w*(?:key|token|secret|password)\w*)=\S+", r"\1=***", line)
    return type(exc).__name__ + ("：" + line[:200] if line else "")


def _stopped_at(reader, start):
    """在父项中途停下（不是附件中途）的续读位置。还没走到起点那一项（重放时停下），位置原样不动，不倒退。"""
    return start if (reader.group, reader.position) == (start[3], start[0]) else (reader.position, 0, False)


def read_site(request) -> dict:
    """site_crawler 插件入口；返回值形状与 read_public 其他插件相同。
    续读位置 cursor = {"i": 第几个父项（详情页 / 已存父文档 / 列表里第几个详情页或记录；分组读取是组内第几条）, "a": 该父项下第几个附件,
    "p": 1 表示这个父项的页面版本已经保存、续读时只为拿附件链接重取页面, "g": 分组读取时第几组（0 不写）}。
    重放到续读位置途中停下，位置原样不动。"""
    reader = _Reader(request)
    error, stop, exhausted, resume = None, "upstream_exhausted", False, None
    start = (0, 0, False, 0)
    try:
        start = _cursor(request)
        reader.position, reader.group = start[0], start[3]
        if request.query in {"by_url", "stored_attachments"}:
            _read_named(request, reader, start)
        elif reader.profile.get("source_type") == "api" or reader.profile.get("source_type") == "table_page":
            _read_site_records(request, reader, start)
        else:
            _read_detail_pages(request, reader, start)
        exhausted = True
    except _Resume as stopped:
        stop, resume = stopped.reason, (reader.position, stopped.attachment, stopped.page_recorded)
    except transport.Stop as stopped:
        # 读到旧的了（增量读取）：没有下一页要接
        stop, resume = stopped.reason, None if stopped.reason == transport.KNOWN else _stopped_at(reader, start)
    except FabricError as exc:
        error, stop = {"code": exc.code, "message": exc.message}, "source_failed"
    except Exception as exc:  # noqa: BLE001 -- 页面抓取失败如实记录，已读原件单独保留
        error = {"code": "source_read_failed", "message": "站点读取失败：" + _why(exc)}
        stop = "source_failed"
        logger.error("站点 {} 读取失败：{}", reader.site, error["message"])
    if reader.unread and stop in ("upstream_exhausted", transport.KNOWN) and error is None:
        # 读到底（或读到旧的）了，但有几条没读到：不能说读完。有版本的是“部分完成”，一个版本也没有的是“失败”（records._close_run）
        first = reader.unread[0]
        error = {"code": "items_unread",
                 "message": f"有 {len(reader.unread)} 条读不到，如 {first['url']}：{first['reason']}"}
        stop = "items_unread"
    return _site_read_result(request, reader, exhausted, stop, error, resume)


def _site_read_result(request, reader, exhausted, stop, error, resume) -> dict:
    next_continuation = None
    if resume and error is None:
        next_continuation = {
            "cursor": json.dumps({"i": resume[0], "a": resume[1], "p": int(resume[2]), **({"g": reader.group} if reader.group else {})}),
            "read_count": reader.group or resume[0],  # 读到第几组 / 第几条
        }
    return {
        "records": [r for r in reader.records if r is not None],
        "complete": False,
        "upstream_pagination_exhausted": exhausted and error is None,
        "stop_reason": stop,
        "error": error,
        "source_total": None,
        "count_drift": None,
        "next_continuation": next_continuation,
        "attachment_coverage": reader.coverage,
        "unread_items": reader.unread,
        "skipped_items": reader.skipped,
    }


def _parsed_text(parsed):
    """解析结果 → (标题, 清洗正文, 发布时间)。"""
    title, clean_text = parsed.get("title") or "", parsed.get("markdown") or ""
    return title, clean_text, source_time(parsed.get("publish_date") or None, "site_publish_date")


def _withdrawal(data, facts):
    """撤下的记录（ops/withdraw_records.py 写的墓碑原件）：facts 带 publication_state 和原因，检索读成 withdrawn，
    知识库撤下（store.index_publication.TOMBSTONES）。"""
    if data.get("publication_state") == "withdrawn":
        facts.update(publication_state="withdrawn", withdraw_reason=data.get("withdraw_reason") or "")
    return facts


def _web_capture_fields(data):
    parsed = data.get("parsed") or {}
    facts = _withdrawal(data, {
        "role": "web_capture",
        "parse_method": parsed.get("parse_method"),
        "attachment_record_ids": [a.get("record_id") for a in data.get("attachments", [])],
        "extra": parsed.get("extra") or {},
    })
    return (facts, *_parsed_text(parsed))


def _site_record_fields(data):
    parsed = data.get("parsed") or {}
    facts = _withdrawal(data, {
        "role": "site_record",
        "parse_method": parsed.get("parse_method"),
        "structured": parsed.get("structured_data") or {},
        "extra": parsed.get("extra") or {},
        "attachment_record_ids": [a.get("record_id") for a in data.get("attachments", [])],
        "source_fields_kept": data.get("raw") is not None,
    })
    return (facts, *_parsed_text(parsed))


def _legacy_capture_fields(data):
    parsed = data.get("parsed") or {}
    facts = {"role": "legacy_capture", "capture_kind": data.get("kind"), "parse_method": "legacy_documents_markdown"}
    return (facts, *_parsed_text(parsed))


def _attachment_fields(data):
    facts = {
        k: data.get(k)
        for k in ("parent_record_id", "file_name", "file_ext", "sha256", "size", "store_path", "discovered_from")
    }
    facts["role"] = "attachment"
    # 所属页面的标题、文号（附件进知识库时写在开头），发布日期就是附件的发布日期（附件自己没有日期）
    parent = data.get("parent") or {}
    for key in ("title", "doc_number", "publish_date"):
        if parent.get(key):
            facts["parent_" + key] = parent[key]
    published = source_time(parent.get("publish_date") or None, "parent_publish_date")
    return facts, data.get("file_name") or "", "", published


# role → (它必须带的 schema 版本, 它的 (facts, 标题, 清洗正文, 发布时间) 投影)
_CAPTURE_ROLES = {
    "web_capture": (WEB_SCHEMA, _web_capture_fields),
    "site_record": (RECORD_SCHEMA, _site_record_fields),
    "legacy_capture": (LEGACY_SCHEMA, _legacy_capture_fields),
    "attachment": (ATT_SCHEMA, _attachment_fields),
}


def _expected_capture_id(role, data):
    """这个 role 的原件必须登记在哪个来源记录号下。"""
    if role == "attachment":
        return attachment_record_id(data.get("parent_record_id") or "", data.get("key") or data.get("url") or "")
    if role in ("site_record", "legacy_capture"):
        return data.get("record_id")
    return data.get("url")


def project_capture(source_id: str, item: ArchiveRecord) -> dict:
    """web_capture / site_record / attachment 原件的确定性投影。附件正文不在这里，由加工层产出。"""
    try:
        data = json.loads(item.raw_content)
    except ValueError:
        raise FabricError("invalid_source_fields", "站点原件不是合法 JSON") from None
    role = data.get("role") if isinstance(data, dict) else None
    kind = _CAPTURE_ROLES.get(role) if isinstance(role, str) else None
    if kind is None or data.get("schema_version") != kind[0]:
        raise FabricError("invalid_source_fields",
                           "站点原件不符合 web_capture / site_record / attachment / legacy_capture 契约")
    facts, title, clean_text, published = kind[1](data)
    if _expected_capture_id(role, data) != item.source_record_id or data.get("site") != source_id:
        raise FabricError("native_id_mismatch", "站点原件的链接或来源与记录不一致")
    if role in ("web_capture", "site_record") and SITE_PROFILES.get(source_id, {}).get("single_clean_text"):
        # 正文只有一份：清洗段从原件 /parsed/markdown 洗出的 derived_clean_text。投影里不再放一份提取器原样输出
        # （以前两份字数不同，下游不知该用哪份）
        clean_text = ""
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
