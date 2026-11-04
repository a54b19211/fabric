"""PubMed 记录的合法全文：PMC 全文 XML、Unpaywall OA PDF，作为该文献的附件版本（唯一附件链的一部分）。

旧 ETL 的 PubMed 抓取器（biz_rules/extractors/pubmed/pubmed_fetcher.py，已删除）默认 fetch_fulltext=True：
有 PMCID 用 PMC efetch 取全文，没取到再用 Unpaywall 找 OA PDF。这里把这两条路收进 PubMed 读取
（biz_rules.history_sources._read_pubmed），按实际许可和可取得层级保存或记缺失，不编：

PMC（有 PMCID 时，一次 efetch db=pmc 请求）。PMC OA Web Service（oa.fcgi）2026-11-04 实测已下线（404），
所以层级按 efetch 返回的 JATS 本身判定：
- 有 <body> 且 <license> 指向 Creative Commons 许可或公共领域 → pmc_fulltext_cc：原件按 sha256 落盘，
  成为附件版本（父记录 PMID，身份“PMID#attachment=efetch 链接”），许可写进 facts.access；
- 有 <body> 但许可不是明确的 CC / 公共领域 → pmc_license_unclear：不保存，记缺失；
- 没有 <body>（出版方不允许下载 XML 全文，PMC 只给前言）→ pmc_fulltext_not_available：记缺失。
Unpaywall（PMC 没得到可保存全文且有 DOI 时）：
- 要联系邮箱 DATAFABRIC_UNPAYWALL_EMAIL；没配 → unpaywall_not_configured，跳过并记缺口；
- best_oa_location（其次 oa_locations）里有 url_for_pdf 且 license 是 cc-* / 公共领域 → 下载
  （core_engine.scrapling_client.download_pdf，同一落盘写法），文件头是 PDF 才成为附件版本；
- 其余（publisher-specific-oa、implied-oa、无 license、不是 OA）→ 记缺失，链接和 license 原值留在覆盖记录里。
每个对外请求发出前都过共享限速 collection.rate 并计入本次读取的请求数、字节数、截止时间预算；预算用完，
剩下的文献记 not_attempted。开关 DATAFABRIC_PUBMED_FULLTEXT=0 关闭（每篇记 fulltext_disabled）。
正文由加工层产出：PMC JATS 由 processing.jats_text（节/段定位），PDF 由 attachment_parse（MinerU，页定位）。
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

from collection import rate
from collection.attachments import attachment_record

PMC_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
UNPAYWALL = "https://api.unpaywall.org/v2/"
XLINK = "{http://www.w3.org/1999/xlink}href"
ALI = "{http://www.niso.org/schemas/ali/1.0/}license_ref"
CC = re.compile(r"creativecommons\.org/(?:licenses/([a-z-]+)/(\d(?:\.\d)?)|publicdomain/(zero|mark)/(\d\.\d))", re.I)
OPEN_UNPAYWALL = re.compile(r"(cc-[a-z-]+|cc0|pd|public-domain)", re.I)


def enabled() -> bool:
    return os.environ.get("DATAFABRIC_PUBMED_FULLTEXT", "1").strip().lower() not in {"0", "false", "off", "no"}


def unpaywall_email() -> str:
    return os.environ.get("DATAFABRIC_UNPAYWALL_EMAIL", "").strip()


def max_file_bytes() -> int:
    return int(os.environ.get("DATAFABRIC_ATTACHMENT_MAX_BYTES", str(100 * 1024 * 1024)))


def run_bytes() -> int:
    return int(os.environ.get("DATAFABRIC_FULLTEXT_RUN_MAX_BYTES", str(256 * 1024 * 1024)))


def pmc_url(pmcid: str) -> str:
    return PMC_EFETCH + "?db=pmc&id=" + pmcid + "&retmode=xml"


class _Stop(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Budget:
    """全文请求的预算：请求数、字节数、截止时间（与 PubMed 元数据请求共用一次读取的上限）。"""

    def __init__(self, *, requests_left, deadline):
        self.requests_left = requests_left
        self.deadline = deadline
        self.requests = 0
        self.bytes = 0
        self.byte_budget = run_bytes()

    def before_request(self, url):
        if self.requests >= self.requests_left:
            raise _Stop("request_budget")
        if self.bytes >= self.byte_budget:
            raise _Stop("byte_budget")
        try:
            rate.wait_for_url(url, deadline=self.deadline)
        except TimeoutError:
            raise _Stop("deadline") from None
        if time.monotonic() >= self.deadline:
            raise _Stop("deadline")
        self.requests += 1

    def file_cap(self):
        return max(0, min(max_file_bytes(), self.byte_budget - self.bytes))

    def timeout(self):
        return max(0.1, min(60.0, self.deadline - time.monotonic()))


class _TooLarge(Exception):
    pass


def _get(client, budget, url, params=None):
    """一次有界 GET：先过闸计数，流式读，超过单文件/本次剩余字节就停。返回 (状态码, 字节)。"""
    budget.before_request(url)
    cap, body = budget.file_cap(), bytearray()
    with client.stream("GET", url, params=params, timeout=budget.timeout()) as response:
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > cap:
                budget.bytes += len(body)
                raise _TooLarge()
            if time.monotonic() >= budget.deadline:
                budget.bytes += len(body)
                raise _Stop("deadline")
        status = response.status_code
    budget.bytes += len(body)
    return status, bytes(body)


def _cc_code(urls):
    for url in urls:
        match = CC.search(url or "")
        if match:
            if match.group(1):
                return "CC " + match.group(1).upper() + " " + match.group(2)
            return "CC0 " + match.group(4) if match.group(3).lower() == "zero" else "Public Domain Mark"
    return None


def jats_access(raw: bytes, pmcid: str) -> dict:
    """PMC efetch 返回的 JATS → 可取得层级与许可（只看原件里写了什么）。"""
    head = raw[:4096]
    if b"<!ENTITY" in head:
        raise ValueError("entity_declaration")
    root = ET.fromstring(raw)
    article = root if root.tag == "article" else root.find("article")
    if article is None:
        return {"tier": "pmc_not_found", "license": None}
    ids = {node.get("pub-id-type"): (node.text or "").strip() for node in article.iter("article-id")}
    found = ids.get("pmcid") or ("PMC" + ids["pmc"] if ids.get("pmc") else "")
    if found and found.upper() != pmcid.upper():
        return {"tier": "pmc_id_mismatch", "license": None}
    urls, types, statements = [], [], []
    for lic in article.iter("license"):
        urls.append(lic.get(XLINK) or "")
        types.append(lic.get("license-type") or "")
        urls.extend((ref.text or "").strip() for ref in lic.iter(ALI))
        statements.extend(" ".join("".join(p.itertext()).split())[:500] for p in lic.iter("license-p"))
    urls = [u for u in urls if u]
    body = article.find("body")
    has_body = body is not None and any("".join(p.itertext()).strip() for p in body.iter("p"))
    code = _cc_code(urls)
    tier = ("pmc_fulltext_not_available" if not has_body
            else "pmc_fulltext_cc" if code else "pmc_license_unclear")
    return {
        "tier": tier,
        "license": code,
        "license_urls": urls,
        "license_types": [t for t in types if t],
        "license_statements": [s for s in statements if s],
        "basis": "PMC efetch JATS <license>（PMC OA Web Service 已下线，不用它判定）",
    }


def _pmc(client, budget, pmid, pmcid):
    from collection.site_crawler import attachment_record_id
    from core_engine.scrapling_client import store_original

    url = pmc_url(pmcid)
    record_id = attachment_record_id(pmid, url)
    entry = {"parent_record_id": pmid, "route": "pmc", "url": url, "record_id": record_id}
    try:
        status, raw = _get(client, budget, PMC_EFETCH, {"db": "pmc", "id": pmcid, "retmode": "xml"})
    except _TooLarge:
        return {**entry, "status": "too_large"}, None
    except _Stop:
        raise
    except Exception as exc:  # noqa: BLE001 -- 全文请求失败是覆盖缺口，不让元数据失败
        return {**entry, "status": "pmc_fetch_failed", "error": type(exc).__name__}, None
    if status != 200:
        return {**entry, "status": "pmc_fetch_failed", "error": "HTTP " + str(status)}, None
    try:
        access = jats_access(raw, pmcid)
    except (ET.ParseError, ValueError):
        return {**entry, "status": "pmc_invalid_xml"}, None
    entry.update(status=access["tier"], license=access["license"])
    if access["tier"] != "pmc_fulltext_cc":
        entry["license_urls"] = access.get("license_urls", [])
        return entry, None
    try:
        stored = store_original(raw, url, "pubmed", ".xml")
    except OSError as exc:
        return {**entry, "status": "store_failed", "error": type(exc).__name__}, None
    entry.update(status="stored", tier=access["tier"], sha256=stored["sha256"], size=stored["size"])
    record = attachment_record(
        "pubmed", record_id=record_id, url=url, parent_record_id=pmid, stored=stored,
        file_name=pmcid + ".xml", file_ext=".xml", discovered_from="pmc_efetch", access=access,
        extra={"pmcid": pmcid},
    )
    return entry, record


def _download(url, *, pmid, cap, timeout):
    from core_engine.scrapling_client import download_pdf

    return download_pdf.fn(url, parent_uuid=pmid, source_site="pubmed", max_bytes=cap, timeout=timeout)


def _unpaywall(client, budget, pmid, doi):
    from collection.site_crawler import attachment_record_id

    base = {"parent_record_id": pmid, "route": "unpaywall", "doi": doi}
    email = unpaywall_email()
    if not email:
        return {**base, "status": "unpaywall_not_configured"}, None
    if not re.fullmatch(r"10\.\d{4,9}/\S{1,300}", doi):
        return {**base, "status": "invalid_doi"}, None
    lookup = UNPAYWALL + quote(doi, safe="/")
    try:
        status, raw = _get(client, budget, lookup, {"email": email})
    except _TooLarge:
        return {**base, "status": "unpaywall_failed", "error": "too_large"}, None
    except _Stop:
        raise
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "unpaywall_failed", "error": type(exc).__name__}, None
    if status == 404:
        return {**base, "status": "unpaywall_not_found"}, None
    if status != 200:
        return {**base, "status": "unpaywall_failed", "error": "HTTP " + str(status)}, None
    try:
        data = json.loads(raw)
    except ValueError:
        return {**base, "status": "unpaywall_failed", "error": "invalid_json"}, None
    locations = [data.get("best_oa_location") or {}] + list(data.get("oa_locations") or [])
    location = next((loc for loc in locations if isinstance(loc, dict) and loc.get("url_for_pdf")), None)
    if not data.get("is_oa") or location is None:
        return {**base, "status": "unpaywall_no_oa_pdf", "oa_status": data.get("oa_status")}, None
    url, license_value = location["url_for_pdf"], location.get("license")
    entry = {**base, "url": url, "license": license_value, "host_type": location.get("host_type"),
             "oa_version": location.get("version"), "record_id": attachment_record_id(pmid, url)}
    if not (isinstance(license_value, str) and OPEN_UNPAYWALL.fullmatch(license_value.strip())):
        return {**entry, "status": "oa_license_unclear"}, None
    budget.before_request(url)
    try:
        info = _download(url, pmid=pmid, cap=budget.file_cap(), timeout=budget.timeout())
    except ValueError as exc:
        status = "too_large" if str(exc) == "attachment_too_large" else "oa_pdf_failed"
        return {**entry, "status": status}, None
    except Exception as exc:  # noqa: BLE001
        return {**entry, "status": "oa_pdf_failed", "error": type(exc).__name__}, None
    budget.bytes += int(info.get("size") or 0)
    if info.get("file_ext") != ".pdf":
        return {**entry, "status": "oa_link_not_pdf"}, None
    stored = {"local_path": info["local_path"], "sha256": info["sha256"], "size": info["size"]}
    access = {"tier": "unpaywall_oa_pdf", "license": license_value, "host_type": location.get("host_type"),
              "oa_version": location.get("version"), "basis": "Unpaywall best_oa_location / oa_locations"}
    record = attachment_record(
        "pubmed", record_id=entry["record_id"], url=url, parent_record_id=pmid, stored=stored,
        file_name=pmid + "_oa.pdf", file_ext=".pdf", discovered_from="unpaywall", access=access,
        extra={"doi": doi},
    )
    return {**entry, "status": "stored", "tier": "unpaywall_oa_pdf", "sha256": stored["sha256"],
            "size": stored["size"]}, record


def attach_fulltext(client, articles, *, requests_left, deadline):
    """articles：[(PMID, PMCID 或 None, DOI 或 None)]。返回 (附件原件, 覆盖记录, 请求数, 字节数)。"""
    records, coverage = [], []
    if not enabled():
        return [], [{"parent_record_id": pmid, "status": "fulltext_disabled"} for pmid, _, _ in articles], 0, 0
    budget = _Budget(requests_left=requests_left, deadline=deadline)
    for position, (pmid, pmcid, doi) in enumerate(articles):
        try:
            if pmcid and re.fullmatch(r"PMC\d{1,12}", pmcid):
                entry, record = _pmc(client, budget, pmid, pmcid)
            else:
                entry = {"parent_record_id": pmid, "route": "pmc", "status": "invalid_pmcid" if pmcid else "no_pmcid"}
                record = None
            coverage.append(entry)
            if record is None:
                if doi:
                    entry, record = _unpaywall(client, budget, pmid, doi)
                    coverage.append(entry)
                else:
                    coverage.append({"parent_record_id": pmid, "route": "unpaywall", "status": "no_doi"})
            if record is not None:
                records.append(record)
        except _Stop as stop:
            coverage.extend({"parent_record_id": later, "status": "not_attempted", "reason": stop.reason}
                            for later, _, _ in articles[position:])
            break
    return records, coverage, budget.requests, budget.bytes
