"""
DrugBank API 分页拉取器

文档：https://docs.drugbank.com/v1/
认证：Authorization: <api_key>  （Header）
端点：GET https://api.drugbank.com/v1/drugs
分页：Link header（RFC 5988），同时响应头含 X-Total-Count

api_cfg 字段（来自 config/sites/drugbank.py 的 "api" 块）：
  - api_key:     DrugBank API Key（必填，或从 DRUGBANK_API_KEY 环境变量读取）
  - region:      us / ca / eu，默认 us
  - q:           关键词搜索，留空拉全量
  - max_records: 最多拉取条数，0 = 全量
  - page_size:   每页条数，默认 50（DrugBank 上限 100）
"""

import os
import re
import time
from typing import Iterator

import httpx
from loguru import logger

BASE_URL = "https://api.drugbank.com/v1"
DEFAULT_PAGE_SIZE = 50
REQUEST_DELAY = 0.5  # 秒，避免触发限速


def iter_drugbank(api_cfg: dict) -> Iterator[dict]:
    """
    DrugBank /drugs 端点分页拉取。
    逐条 yield 原始 drug record dict。
    """
    api_key = api_cfg.get("api_key") or os.getenv("DRUGBANK_API_KEY", "")
    if not api_key:
        logger.error("[DrugBank] 未配置 API Key，请在 .env 中设置 DRUGBANK_API_KEY")
        return

    region = api_cfg.get("region", "us")
    q = api_cfg.get("q", "")
    max_records = api_cfg.get("max_records", 0)
    page_size = min(api_cfg.get("page_size", DEFAULT_PAGE_SIZE), 100)

    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "X-DrugBank-Region": region,
    }

    params: dict = {"per_page": page_size}
    if q:
        params["q"] = q

    url = f"{BASE_URL}/drugs"
    fetched = 0

    logger.info(f"[DrugBank] 开始拉取，region={region}, q={q!r}, max={max_records or '全量'}")

    while url:
        if max_records > 0 and fetched >= max_records:
            break

        # 最后一页可能不需要那么多条
        if max_records > 0:
            params["per_page"] = min(page_size, max_records - fetched)

        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(f"[DrugBank] HTTP {e.response.status_code}: {e.response.text[:200]}")
            break
        except Exception as e:
            logger.error(f"[DrugBank] 请求异常: {e}")
            break

        records = resp.json()
        if not isinstance(records, list) or not records:
            logger.info("[DrugBank] 返回空列表，拉取结束")
            break

        total_count = int(resp.headers.get("X-Total-Count", 0))

        for record in records:
            yield _normalize(record)
            fetched += 1
            if max_records > 0 and fetched >= max_records:
                break

        logger.info(f"[DrugBank] 已拉取 {fetched}/{total_count or '?'} 条")

        # 从 Link header 解析下一页 URL
        url = _parse_next_link(resp.headers.get("Link", ""))
        params = {}  # 下一页 URL 已含参数，清空 params 避免重复
        time.sleep(REQUEST_DELAY)

    logger.info(f"[DrugBank] 拉取完成，共 {fetched} 条")


def _parse_next_link(link_header: str) -> str:
    """
    解析 RFC 5988 Link header，提取 rel="next" 的 URL。
    示例：<https://api.drugbank.com/v1/drugs?page=2&per_page=50>; rel="next"
    """
    if not link_header:
        return ""
    for part in link_header.split(","):
        part = part.strip()
        m = re.match(r'<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return ""


def _normalize(record: dict) -> dict:
    """将 DrugBank API 原始记录规范化，保留核心字段。"""
    drugbank_id = record.get("drugbank_id", "")
    name = record.get("name", "")

    # 药品分类
    categories = record.get("categories", [])
    category_names = [c.get("name", "") for c in categories if c.get("name")]

    # ATC 编码
    atc_codes = record.get("atc_codes", [])
    atc_list = [a.get("code", "") for a in atc_codes if a.get("code")]

    # 适应症 / 描述
    description = record.get("description", "")
    indication = record.get("indication", "")

    # 药物相互作用数量（不展开，避免数据量过大）
    interactions_count = len(record.get("drug_interactions", []))

    return {
        "drugbank_id": drugbank_id,
        "name": name,
        "description": description,
        "indication": indication,
        "drug_type": record.get("type", ""),
        "state": record.get("state", ""),
        "groups": record.get("groups", []),          # approved / experimental / withdrawn 等
        "categories": category_names,
        "atc_codes": atc_list,
        "cas_number": record.get("cas_number", ""),
        "unii": record.get("unii", ""),
        "average_mass": record.get("average_mass", ""),
        "interactions_count": interactions_count,
        "synonyms": record.get("synonyms", [])[:10],  # 只取前 10 个同义词
        "source_url": f"https://go.drugbank.com/drugs/{drugbank_id}" if drugbank_id else "",
        "updated": record.get("updated", ""),
        "created": record.get("created", ""),
    }
