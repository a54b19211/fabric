"""PubMed E-utilities API 分页拉取器

全文获取优先级：
  1. PMC (PubMed Central) — 有 pmcid 则通过 efetch 拿全文 XML，转 Markdown，完全免费
  2. Unpaywall API       — 有 DOI 则查合法 OA PDF 链接，下载 PDF 作为附件
  3. 仅摘要              — 两者都没有时，记录 fulltext_status="abstract_only"

字段说明（yield 的 dict 中 structured_data 额外字段）：
  - fulltext_status: "pmc_fulltext" | "unpaywall_pdf" | "abstract_only"
  - pmc_url:         PMC 全文页面链接（有 pmcid 时）
  - oa_pdf_url:      Unpaywall 找到的 OA PDF 链接（有时）
"""
import time
import uuid as _uuid
from typing import Iterator

import httpx
from loguru import logger

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PAGE_SIZE = 100
REQUEST_DELAY = 0.4   # NCBI 无 key 限速 3 req/s
UNPAYWALL_EMAIL = "crawler@example.com"  # Unpaywall 要求提供联系邮箱


def _make_uuid(seed: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, seed))


# ---------------------------------------------------------------------------
# PMC 全文获取
# ---------------------------------------------------------------------------

def _fetch_pmc_fulltext(pmcid: str, api_key: str = "") -> str:
    """
    通过 efetch 拉取 PMC 全文 XML，提取纯文本段落，返回 Markdown 字符串。
    失败返回空字符串。
    """
    params = {
        "db": "pmc",
        "id": pmcid,
        "rettype": "full",
        "retmode": "xml",
    }
    if api_key:
        params["api_key"] = api_key

    try:
        resp = httpx.get(f"{BASE_URL}/efetch.fcgi", params=params, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[PubMed] PMC efetch 失败 {pmcid}: {e}")
        return ""

    # 用 xml.etree 提取正文段落，避免引入 lxml 依赖
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        paragraphs = []
        for p in root.iter("p"):
            text = "".join(p.itertext()).strip()
            if text:
                paragraphs.append(text)
        if paragraphs:
            logger.info(f"[PubMed] PMC 全文提取成功 {pmcid}，段落数: {len(paragraphs)}")
            return "\n\n".join(paragraphs)
    except Exception as e:
        logger.warning(f"[PubMed] PMC XML 解析失败 {pmcid}: {e}")

    return ""


# ---------------------------------------------------------------------------
# Unpaywall 查询
# ---------------------------------------------------------------------------

def _query_unpaywall(doi: str) -> str:
    """
    查询 Unpaywall API，返回最佳 OA PDF 链接，找不到返回空字符串。
    Unpaywall 要求 email 参数，用于联系配额管理。
    """
    if not doi:
        return ""
    url = f"https://api.unpaywall.org/v2/{doi}"
    try:
        resp = httpx.get(url, params={"email": UNPAYWALL_EMAIL}, timeout=15)
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"[PubMed] Unpaywall 查询失败 doi={doi}: {e}")
        return ""

    # 优先找 best_oa_location 的 url_for_pdf
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf", "") or ""
    if pdf_url:
        logger.info(f"[PubMed] Unpaywall 找到 OA PDF: {pdf_url}")
        return pdf_url

    # 兜底：遍历所有 oa_locations
    for loc in data.get("oa_locations", []):
        pdf_url = loc.get("url_for_pdf", "") or ""
        if pdf_url:
            logger.info(f"[PubMed] Unpaywall 兜底找到 OA PDF: {pdf_url}")
            return pdf_url

    return ""


# ---------------------------------------------------------------------------
# esummary 字段规范化
# ---------------------------------------------------------------------------

def _normalize(pmid: str, s: dict) -> dict:
    """将 esummary 单条记录规范化，提取 pmcid、doi 等关键字段。"""
    authors_raw = s.get("authors", [])
    authors = [a.get("name", "") for a in authors_raw if a.get("name")]

    # DOI：elocationid 字段格式为 "doi: 10.xxxx/xxx"
    doi_raw = s.get("elocationid", "")
    doi = doi_raw.replace("doi: ", "").strip() if "doi" in doi_raw.lower() else ""

    # pmcid：从 articleids 列表中找 idtype=="pmcid"
    pmcid = ""
    for id_item in s.get("articleids", []):
        if id_item.get("idtype") == "pmcid":
            val = id_item.get("value", "")
            # 格式可能是 "PMC1234567" 或纯数字
            pmcid = val if val.startswith("PMC") else (f"PMC{val}" if val else "")
            break

    return {
        "pmid": pmid,
        "title": s.get("title", ""),
        "abstract": "",  # ESummary不提供真实摘要，由下方EFetch补入
        "publish_date": s.get("pubdate", ""),
        "journal_name": s.get("fulljournalname") or s.get("source", ""),
        "authors": authors,
        "doi": doi,
        "pmcid": pmcid,
        "keywords": [],
        "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


def _fetch_pubmed_details(ids, api_key=""):
    """EFetch原生摘要和更正关系；缺请求PMID不得伪装成无摘要。"""
    import xml.etree.ElementTree as ET
    params = {"db": "pubmed", "retmode": "xml", "id": ",".join(ids)}
    if api_key:
        params["api_key"] = api_key
    try:
        response = httpx.get(f"{BASE_URL}/efetch.fcgi", params=params, timeout=60)
        response.raise_for_status()
        if len(response.content) > 8 * 1024 * 1024:
            raise ValueError("response_budget")
        root = ET.fromstring(response.text)
        if root.tag != "PubmedArticleSet":
            raise ValueError("invalid_root")
        result = {}
        for article in root.findall("PubmedArticle"):
            pmid = article.findtext("./MedlineCitation/PMID")
            if pmid not in ids or pmid in result:
                raise ValueError("unexpected_or_duplicate_pmid")
            abstract = "\n".join("".join(part.itertext()).strip() for part in
                                  article.findall("./MedlineCitation/Article/Abstract/AbstractText"))
            result[pmid] = {"abstract": abstract,
                "publication_types": [part.text for part in article.findall("./MedlineCitation/Article/PublicationTypeList/PublicationType") if part.text],
                "correction_links": [{"ref_type": part.attrib.get("RefType"), "pmid": part.findtext("PMID")}
                                     for part in article.findall("./MedlineCitation/CommentsCorrectionsList/CommentsCorrections")]}
        if set(result) != set(ids):
            raise ValueError("missing_pmid")
        return result
    except Exception:
        raise RuntimeError("PubMed EFetch摘要原件读取失败，未将缺失摘要补为空成功") from None


# ---------------------------------------------------------------------------
# 主生成器
# ---------------------------------------------------------------------------

def iter_pubmed(api_cfg: dict) -> Iterator[dict]:
    """
    PubMed 分页拉取，并尝试获取全文。

    api_cfg 字段：
      - query:            PubMed 检索式
      - max_records:      最多拉取条数，0 = 全量
      - api_key:          NCBI API Key（可选，有则速率提升至 10 req/s）
      - db:               默认 "pubmed"
      - unpaywall_email:  Unpaywall 联系邮箱（可选，覆盖默认值）
      - fetch_fulltext:   是否尝试获取全文，默认 True
    """
    query = api_cfg.get("query", "drug safety[Title]")
    max_records = api_cfg.get("max_records", 200)
    if not isinstance(max_records, int) or not 1 <= max_records <= 10000:
        raise ValueError("PubMed仅支持1至10000条有界检索；不得用0冒充完整库")
    api_key = api_cfg.get("api_key", "")
    db = api_cfg.get("db", "pubmed")
    fetch_fulltext = api_cfg.get("fetch_fulltext", True)

    global UNPAYWALL_EMAIL
    if api_cfg.get("unpaywall_email"):
        UNPAYWALL_EMAIL = api_cfg["unpaywall_email"]

    def _base_params() -> dict:
        p = {"db": db, "retmode": "json"}
        if api_key:
            p["api_key"] = api_key
        return p

    # ── Step 1: esearch ──────────────────────────────────────────
    search_params = _base_params()
    search_params.update({
        "term": query,
        "retmax": min(max_records, 10000) if max_records > 0 else 10000,
        "usehistory": "y",
    })

    logger.info(f"[PubMed] esearch: {query!r}")
    try:
        resp = httpx.get(f"{BASE_URL}/esearch.fcgi", params=search_params, timeout=30)
        resp.raise_for_status()
        search_data = resp.json().get("esearchresult", {})
    except Exception:
        raise RuntimeError("PubMed esearch读取失败，不能视为零命中") from None

    if "count" not in search_data or "idlist" not in search_data:
        raise RuntimeError("PubMed esearch结构不完整")
    total = int(search_data["count"])
    web_env = search_data.get("webenv", "")
    query_key = search_data.get("querykey", "")
    id_list: list[str] = search_data.get("idlist", [])

    limit = min(max_records, total)
    logger.info(f"[PubMed] 命中 {total} 条，本次拉取 {limit}")

    if total == 0:
        return
    if not id_list and not web_env:
        raise RuntimeError("PubMed声明有命中但未返回ID或历史游标")

    # ── Step 2: esummary 分批拉取 ────────────────────────────────
    fetched = 0
    retstart = 0

    while fetched < limit:
        batch_size = min(PAGE_SIZE, limit - fetched)

        summary_params = _base_params()
        summary_params.update({"retstart": retstart, "retmax": batch_size})

        if web_env and query_key:
            summary_params["WebEnv"] = web_env
            summary_params["query_key"] = query_key
        else:
            batch_ids = id_list[retstart: retstart + batch_size]
            if not batch_ids:
                raise RuntimeError("PubMed本批ID耗尽但声明范围尚未读完")
            summary_params["id"] = ",".join(batch_ids)

        try:
            resp = httpx.get(f"{BASE_URL}/esummary.fcgi", params=summary_params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            raise RuntimeError("PubMed esummary读取失败，不能视为EOF") from None

        result = data.get("result", {})
        uids: list[str] = result.get("uids", [])

        if not uids:
            raise RuntimeError("PubMed esummary在声明范围内返回意外空页")
        time.sleep(REQUEST_DELAY)
        details = _fetch_pubmed_details(uids, api_key)

        for uid in uids:
            article = result.get(uid, {})
            if not article:
                raise RuntimeError("PubMed摘要元数据缺本批请求记录")
            record = _normalize(uid, article)
            record.update(details[uid])
            yield _build_result(record, api_key, fetch_fulltext)
            fetched += 1

        logger.info(f"[PubMed] 已拉取 {fetched}/{min(limit, total)}")
        retstart += batch_size
        time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# 构建最终 yield 结果
# ---------------------------------------------------------------------------

def _build_result(record: dict, api_key: str, fetch_fulltext: bool) -> dict:
    """
    根据 pmcid / doi 尝试获取全文，构建标准输出 dict。
    """
    pmid = record["pmid"]
    pmcid = record.get("pmcid", "")
    doi = record.get("doi", "")
    title = record.get("title", "")
    abstract = record.get("abstract", "")

    fulltext_body = ""
    fulltext_status = "abstract_only"
    oa_pdf_url = ""
    attachment_links = []

    if fetch_fulltext:
        # 路径 1：PMC 全文
        if pmcid:
            time.sleep(REQUEST_DELAY)
            fulltext_body = _fetch_pmc_fulltext(pmcid, api_key)
            if fulltext_body:
                fulltext_status = "pmc_fulltext"

        # 路径 2：Unpaywall OA PDF（PMC 没拿到全文时才查）
        if not fulltext_body and doi:
            time.sleep(0.2)
            oa_pdf_url = _query_unpaywall(doi)
            if oa_pdf_url:
                fulltext_status = "unpaywall_pdf"
                attachment_links = [{"name": f"{pmid}_fulltext.pdf", "url": oa_pdf_url}]

        if fulltext_status == "abstract_only":
            logger.info(f"[PubMed] 无合法全文 pmid={pmid} doi={doi or '无'} pmcid={pmcid or '无'}")

    # 组装 Markdown
    md_lines = [f"# {title}", ""]
    md_lines += [
        f"**pmid**: {pmid}",
        f"**title**: {title}",
        f"**abstract**: {abstract}",
        f"**publish_date**: {record.get('publish_date', '')}",
        f"**journal_name**: {record.get('journal_name', '')}",
        f"**authors**: {record.get('authors', [])}",
        f"**doi**: {doi}",
        f"**pmcid**: {pmcid}",
        f"**keywords**: {record.get('keywords', [])}",
        f"**source_url**: {record.get('source_url', '')}",
        f"**fulltext_status**: {fulltext_status}",
    ]
    if oa_pdf_url:
        md_lines.append(f"**oa_pdf_url**: {oa_pdf_url}")
    if fulltext_body:
        md_lines += ["", "---", "", "## Full Text", "", fulltext_body]

    return {
        "uuid": _make_uuid(f"pubmed:{pmid}"),
        "title": title,
        "publish_date": record.get("publish_date", ""),
        "markdown": "\n".join(md_lines),
        "structured_data": {
            "title": title,
            "publish_date": record.get("publish_date", ""),
            "category": "医学文献",
            "summary": abstract[:500] if abstract else "",
            "drug_name": "",
            "manufacturer": "",
            "fulltext_status": fulltext_status,
            "pmcid": pmcid,
            "oa_pdf_url": oa_pdf_url,
            "publication_types": record.get("publication_types", []),
            "correction_links": record.get("correction_links", []),
        },
        "sniffed_meta": {
            "doi": doi,
            "journal_name": record.get("journal_name", ""),
        },
        "attachment_links": attachment_links,
        "source_url": record.get("source_url", ""),
        "source_site": "pubmed",
        "parse_method": f"pubmed_api+{fulltext_status}",
    }
