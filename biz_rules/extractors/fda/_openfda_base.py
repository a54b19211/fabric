"""
openFDA API 分页基础函数。
所有 openFDA 端点共用同一套分页机制，在这里统一实现。

两种分页模式：
- search_after（推荐）：通过响应的 Link 头获取下一页，无记录数限制。
  需在 api_cfg 中设置 sort 字段，如 "sort": "effective_time:asc"。
- skip/limit（传统）：受 openFDA 25000 skip 硬限制，适合小批量数据。
"""

import re
import time
from typing import Iterator

import httpx
from loguru import logger

OPENFDA_BASE = "https://api.fda.gov"
PAGE_SIZE = 100
MAX_SKIP = 25000
REQUEST_DELAY = 0.5
NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def iter_openfda(api_cfg: dict) -> Iterator[dict]:
    """
    通用 openFDA 分页拉取。
    api_cfg 来自站点配置的 "api" 字段，需包含：
      - endpoint:    如 "/drug/enforcement.json"
      - search:      查询字符串，空则拉全量
      - max_records: 最多拉取条数，0 = 全量
      - sort:       （可选）排序字段，如 "effective_time:asc"。
                    设置后走 search_after 深分页，绕过 25000 限制。
    """
    sort = api_cfg.get("sort")
    if sort:
        yield from _iter_search_after(api_cfg)
    else:
        yield from _iter_skip(api_cfg)


# ── search_after 深分页（无记录数限制）────────────────────────────────

def _iter_search_after(api_cfg: dict) -> Iterator[dict]:
    """通过 Link 头的 search_after 参数无限深分页。"""
    endpoint = api_cfg["endpoint"]
    search = api_cfg.get("search", "")
    max_records = api_cfg.get("max_records", 0)
    sort = api_cfg["sort"]

    base_url = f"{OPENFDA_BASE}{endpoint}"
    params = {"limit": PAGE_SIZE, "sort": sort}
    if search:
        params["search"] = search

    url = None  # None = 首次请求用 base_url+params
    fetched = 0

    while True:
        if url:
            resp = httpx.get(url, timeout=30)
        else:
            resp = httpx.get(base_url, params=params, timeout=30)

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(f"[openFDA] HTTP 错误: {e}")
            break
        except Exception as e:
            logger.error(f"[openFDA] 请求异常: {e}")
            break

        data = resp.json()
        results = data.get("results", [])
        total = data.get("meta", {}).get("results", {}).get("total", 0)

        if not results:
            break

        for record in results:
            yield record
            fetched += 1
            if max_records > 0 and fetched >= max_records:
                return

        logger.info(f"[openFDA] {endpoint} | 已拉取 {fetched}/{total} 条")

        if max_records > 0 and fetched >= max_records:
            break

        # 从 Link 头提取下一页 URL
        link_header = resp.headers.get("Link", "")
        match = NEXT_LINK_RE.search(link_header)
        if match:
            url = match.group(1)
        else:
            break

        time.sleep(REQUEST_DELAY)


# ── skip-based 分页（受 25000 限制）──────────────────────────────────

def _iter_skip(api_cfg: dict) -> Iterator[dict]:
    endpoint = api_cfg.get("endpoint", "")
    search = api_cfg.get("search", "")
    max_records = api_cfg.get("max_records", 0)

    url = f"{OPENFDA_BASE}{endpoint}"
    fetched = 0
    skip = 0

    while True:
        limit = PAGE_SIZE
        if max_records > 0:
            remaining = max_records - fetched
            if remaining <= 0:
                break
            limit = min(PAGE_SIZE, remaining)

        if skip >= MAX_SKIP:
            logger.warning(
                f"[openFDA] skip={skip} 已达硬限制 {MAX_SKIP}，"
                "设置 sort 字段可用 search_after 深分页拉取全量"
            )
            break

        params: dict = {"limit": limit, "skip": skip}
        if search:
            params["search"] = search

        try:
            resp = httpx.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info(f"[openFDA] 分页结束（404）: skip={skip}")
                break
            logger.error(f"[openFDA] HTTP 错误: {e}")
            break
        except Exception as e:
            logger.error(f"[openFDA] 请求异常: {e}")
            break

        data = resp.json()
        results = data.get("results", [])
        total = data.get("meta", {}).get("results", {}).get("total", 0)

        if not results:
            break

        for record in results:
            yield record
            fetched += 1

        logger.info(f"[openFDA] {endpoint} | 已拉取 {fetched}/{total} 条")

        if fetched >= total or (max_records > 0 and fetched >= max_records):
            break

        skip += len(results)
        time.sleep(REQUEST_DELAY)
