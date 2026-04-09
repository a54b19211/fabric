"""PubMed E-utilities API 分页拉取器

流程：
  1. esearch  → 获取 PMID 列表（JSON，支持 WebEnv 服务器端缓存）
  2. esummary → 批量拉取文章摘要信息（JSON，字段齐全）
"""
import time
from typing import Iterator

import httpx
from loguru import logger

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PAGE_SIZE = 100
REQUEST_DELAY = 0.4  # NCBI 无 key 限速 3 req/s，留余量


def iter_pubmed(api_cfg: dict) -> Iterator[dict]:
    """
    PubMed 分页拉取。api_cfg 字段：
      - query:       PubMed 检索式
      - max_records: 最多拉取条数，0 = 全量
      - api_key:     NCBI API Key（可选）
      - db:          默认 "pubmed"
    """
    query = api_cfg.get("query", "drug safety[Title]")
    max_records = api_cfg.get("max_records", 200)
    api_key = api_cfg.get("api_key", "")
    db = api_cfg.get("db", "pubmed")

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
    except Exception as e:
        logger.error(f"[PubMed] esearch 失败: {e}")
        return

    total = int(search_data.get("count", 0))
    web_env = search_data.get("webenv", "")
    query_key = search_data.get("querykey", "")
    id_list: list[str] = search_data.get("idlist", [])

    limit = max_records if max_records > 0 else total
    logger.info(f"[PubMed] 命中 {total} 条，本次拉取 {limit}")

    if not id_list and not web_env:
        logger.warning("[PubMed] 未获取到任何 PMID")
        return

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
                break
            summary_params["id"] = ",".join(batch_ids)

        try:
            resp = httpx.get(f"{BASE_URL}/esummary.fcgi", params=summary_params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"[PubMed] esummary 失败 (retstart={retstart}): {e}")
            break

        result = data.get("result", {})
        uids: list[str] = result.get("uids", [])

        if not uids:
            logger.warning(f"[PubMed] esummary 返回空 (retstart={retstart})")
            break

        for uid in uids:
            article = result.get(uid, {})
            if article:
                yield _normalize(uid, article)
                fetched += 1

        logger.info(f"[PubMed] 已拉取 {fetched}/{min(limit, total)}")
        retstart += batch_size
        time.sleep(REQUEST_DELAY)


def _normalize(pmid: str, s: dict) -> dict:
    """将 esummary 单条记录规范化为系统标准字段。"""
    authors_raw = s.get("authors", [])
    authors = [a.get("name", "") for a in authors_raw if a.get("name")]

    # DOI：elocationid 字段格式为 "doi: 10.xxxx/xxx"
    doi_raw = s.get("elocationid", "")
    doi = doi_raw.replace("doi: ", "").strip() if "doi" in doi_raw.lower() else ""

    return {
        "pmid": pmid,
        "title": s.get("title", ""),
        "abstract": s.get("summary", ""),
        "publish_date": s.get("pubdate", ""),
        "journal_name": s.get("fulljournalname") or s.get("source", ""),
        "authors": authors,
        "doi": doi,
        "keywords": [],
        "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }
