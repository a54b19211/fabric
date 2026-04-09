"""
openFDA API 分页基础函数。
所有 openFDA 端点共用同一套分页机制，在这里统一实现。
"""

import time
from typing import Iterator

import httpx
from loguru import logger

OPENFDA_BASE = "https://api.fda.gov"
PAGE_SIZE = 100
REQUEST_DELAY = 0.5  # 秒


def iter_openfda(api_cfg: dict) -> Iterator[dict]:
    """
    通用 openFDA 分页拉取。
    api_cfg 来自站点配置的 "api" 字段，需包含：
      - endpoint:    如 "/drug/enforcement.json"
      - search:      查询字符串，空则拉全量
      - max_records: 最多拉取条数，0 = 全量
    """
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
