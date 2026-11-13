"""PubMed 记录的合法全文：PMC 全文 XML、Unpaywall OA PDF，作为该文献的附件版本（唯一附件链的一部分）。

旧 ETL 的 PubMed 抓取器（已删除）默认取全文：有 PMCID 用 PMC efetch，没取到再用 Unpaywall 找 OA PDF。这里把
这两条路收进 PubMed 读取（fetch.sources._read_pubmed），按原件写明的许可保存或记缺口，不编：

PMC（有 PMCID 时，一次 efetch db=pmc 请求）。PMC OA Web Service（oa.fcgi）2026-11-04 实测已下线（404），
所以按 efetch 返回的 JATS 本身判定：
- 有 <body> 且 <license> 指向 Creative Commons 许可或公共领域 → 保存：原件按 sha256 落盘，成为附件版本
  （父记录 PMID，身份“PMID#attachment=efetch 链接”）；
- 有 <body> 但许可不是明确的 CC / 公共领域（出版方专用、implied 等）→ pmc_license_unclear：不保存，记待核缺口；
- 没有 <body>（出版方不允许下载 XML 全文）→ pmc_fulltext_not_available。
Unpaywall（PMC 没得到可保存全文且有 DOI 时）：要联系邮箱 DATAFABRIC_UNPAYWALL_EMAIL，没配记 unpaywall_not_configured；
best_oa_location（其次 oa_locations）有 url_for_pdf 且 license 是 cc-* / 公共领域才下载，文件头是 PDF 才成为
附件版本；publisher-specific-oa、implied-oa、无 license → oa_license_unclear（待核缺口，链接和许可原值留在覆盖记录）。

许可按篇保存：具体许可（如 "CC BY-NC 4.0"）、条款（BY / NC / ND / SA）、原文链接和依据都写进附件版本的
access，不把所有 cc-* 当成同一种可用范围；能不能用于某个业务用途由使用方按条款判断（PMC 许可因篇而异）。
请求、字节、截止时间和版本数都走本次读取的同一个预算（fetch.transport）：保存全文前先确认还有版本名额，
没有就停（PubMed 读取给出“这一篇只差全文”的续读位置）。开关 DATAFABRIC_PUBMED_FULLTEXT=0 关闭。
正文由加工层产出：PMC JATS 由 clean.jats_text（节/段定位），PDF 由 attachment_parse（MinerU，页定位）。
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from fetch import transport
from fetch.attachments import attachment_record

PMC_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
UNPAYWALL = "https://api.unpaywall.org/v2/"
XLINK = "{http://www.w3.org/1999/xlink}href"
ALI = "{http://www.niso.org/schemas/ali/1.0/}license_ref"
CC = re.compile(r"creativecommons\.org/(?:licenses/([a-z-]+)/(\d(?:\.\d)?)|publicdomain/(zero|mark)/(\d\.\d))", re.I)
OPEN_UNPAYWALL = re.compile(r"(cc-[a-z-]+|cc0|pd|public-domain)", re.I)
XML_BYTES = 32 * 1024 * 1024
LOOKUP_BYTES = 1024 * 1024


def enabled() -> bool:
    return os.environ.get("DATAFABRIC_PUBMED_FULLTEXT", "1").strip().lower() not in {"0", "false", "off", "no"}


def unpaywall_email() -> str:
    return os.environ.get("DATAFABRIC_UNPAYWALL_EMAIL", "").strip()


def max_file_bytes() -> int:
    return int(os.environ.get("DATAFABRIC_ATTACHMENT_MAX_BYTES", str(100 * 1024 * 1024)))


def pmc_url(pmcid: str) -> str:
    return PMC_EFETCH + "?db=pmc&id=" + pmcid + "&retmode=xml"


def conditions(license_code: str | None) -> list[str]:
    """许可条款：CC BY-NC-ND 4.0 / cc-by-nc-nd → ["BY", "NC", "ND"]；CC0、公共领域 → []。"""
    text = (license_code or "").upper().replace("CC-", "CC ").replace("-", " ")
    return [term for term in ("BY", "NC", "ND", "SA") if re.search(r"\b" + term + r"\b", text)]


def _cc_code(urls):
    for url in urls:
        match = CC.search(url or "")
        if match:
            if match.group(1):
                return "CC " + match.group(1).upper() + " " + match.group(2)
            return "CC0 " + match.group(4) if match.group(3).lower() == "zero" else "Public Domain Mark"
    return None


def _licenses(article):
    """(license URLs, license types, license statements) of every <license> of the article, in document order."""
    urls, types, statements = [], [], []
    for lic in article.iter("license"):
        urls.append(lic.get(XLINK) or "")
        types.append(lic.get("license-type") or "")
        urls.extend((ref.text or "").strip() for ref in lic.iter(ALI))
        statements.extend(" ".join("".join(p.itertext()).split())[:500] for p in lic.iter("license-p"))
    return urls, types, statements


def _has_body(article) -> bool:
    """The article has a <body> with at least one non-empty paragraph."""
    body = article.find("body")
    return body is not None and any("".join(p.itertext()).strip() for p in body.iter("p"))


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
    urls, types, statements = _licenses(article)
    urls = [u for u in urls if u]
    has_body = _has_body(article)
    code = _cc_code(urls)
    tier = ("pmc_fulltext_not_available" if not has_body
            else "pmc_fulltext_cc" if code else "pmc_license_unclear")
    return {
        "tier": tier,
        "license": code,
        "conditions": conditions(code) if code else None,
        "license_urls": urls,
        "license_types": [t for t in types if t],
        "license_statements": [s for s in statements if s],
        "basis": "PMC efetch JATS <license>（PMC OA Web Service 已下线，不用它判定）",
    }


def _pmc(client, budget, pmid, pmcid):
    from fetch.site_crawler import attachment_record_id
    from fetch.web_client import store_original

    url = pmc_url(pmcid)
    record_id = attachment_record_id(pmid, url)
    entry = {"parent_record_id": pmid, "route": "pmc", "url": url, "record_id": record_id}
    budget.need_record()
    try:
        status, raw = transport.get(client, PMC_EFETCH, {"db": "pmc", "id": pmcid, "retmode": "xml"},
                                    cap=min(XML_BYTES, max_file_bytes()), timeout=60.0)
    except ValueError as exc:
        if str(exc) == "response_too_large":
            return {**entry, "status": "too_large"}, None
        return {**entry, "status": "pmc_fetch_failed", "error": type(exc).__name__}, None
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
        if access["tier"] == "pmc_license_unclear":
            entry["needs_review"] = True
        return entry, None
    try:
        stored = store_original(raw, url, "pubmed", ".xml")
    except OSError as exc:
        return {**entry, "status": "store_failed", "error": type(exc).__name__}, None
    budget.take_record()
    entry.update(status="stored", tier=access["tier"], sha256=stored["sha256"], size=stored["size"])
    record = attachment_record(
        "pubmed", record_id=record_id, url=url, parent_record_id=pmid, stored=stored,
        file_name=pmcid + ".xml", file_ext=".xml", discovered_from="pmc_efetch", access=access,
        extra={"pmcid": pmcid},
    )
    return entry, record


def _download(url, *, pmid, cap, timeout):
    from fetch.web_client import download_pdf

    return download_pdf(url, parent_uuid=pmid, source_site="pubmed", max_bytes=cap, timeout=timeout)


def _unpaywall(client, budget, pmid, doi):
    from fetch.site_crawler import attachment_record_id

    base = {"parent_record_id": pmid, "route": "unpaywall", "doi": doi}
    email = unpaywall_email()
    if not email:
        return {**base, "status": "unpaywall_not_configured"}, None
    if not re.fullmatch(r"10\.\d{4,9}/\S{1,300}", doi):
        return {**base, "status": "invalid_doi"}, None
    budget.need_record()
    try:
        status, raw = transport.get(client, UNPAYWALL + quote(doi, safe="/"), {"email": email}, cap=LOOKUP_BYTES)
    except Exception as exc:  # noqa: BLE001 -- 预算停止是 BaseException，不在这里
        return {**base, "status": "unpaywall_failed", "error": str(exc)[:40] or type(exc).__name__}, None
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
        return {**entry, "status": "oa_license_unclear", "needs_review": True}, None
    return _store_oa_pdf(budget, pmid, doi, entry, location)


def _store_oa_pdf(budget, pmid, doi, entry, location):
    """Download the open-licence PDF Unpaywall named and make it the attachment version."""
    url, license_value = location["url_for_pdf"], location.get("license")
    try:
        info = _download(url, pmid=pmid, cap=max(0, min(max_file_bytes(), budget.max_bytes - budget.bytes)),
                         timeout=budget.timeout(60.0))
    except ValueError as exc:
        status = "too_large" if str(exc) == "attachment_too_large" else "oa_pdf_failed"
        return {**entry, "status": status}, None
    except Exception as exc:  # noqa: BLE001
        return {**entry, "status": "oa_pdf_failed", "error": type(exc).__name__}, None
    if info.get("file_ext") != ".pdf":
        return {**entry, "status": "oa_link_not_pdf"}, None
    budget.take_record()
    stored = {"local_path": info["local_path"], "sha256": info["sha256"], "size": info["size"]}
    access = {"tier": "unpaywall_oa_pdf", "license": license_value, "conditions": conditions(license_value),
              "host_type": location.get("host_type"), "oa_version": location.get("version"),
              "basis": "Unpaywall best_oa_location / oa_locations"}
    record = attachment_record(
        "pubmed", record_id=entry["record_id"], url=url, parent_record_id=pmid, stored=stored,
        file_name=pmid + "_oa.pdf", file_ext=".pdf", discovered_from="unpaywall", access=access,
        extra={"doi": doi},
    )
    return {**entry, "status": "stored", "tier": "unpaywall_oa_pdf", "sha256": stored["sha256"],
            "size": stored["size"]}, record


def fulltext(client, pmid, pmcid, doi):
    """一篇文献的全文 → (附件原件列表, 覆盖记录)。预算停止（transport.Stop）向上抛，由 PubMed 读取给续读位置。"""
    if not enabled():
        return [], [{"parent_record_id": pmid, "status": "fulltext_disabled"}]
    budget = transport.current()
    coverage = []
    if pmcid and re.fullmatch(r"PMC\d{1,12}", pmcid):
        entry, record = _pmc(client, budget, pmid, pmcid)
    else:
        entry, record = {"parent_record_id": pmid, "route": "pmc", "status": "invalid_pmcid" if pmcid else "no_pmcid"}, None
    coverage.append(entry)
    if record is None:
        if doi:
            entry, record = _unpaywall(client, budget, pmid, doi)
            coverage.append(entry)
        else:
            coverage.append({"parent_record_id": pmid, "route": "unpaywall", "status": "no_doi"})
    return ([record] if record is not None else []), coverage
