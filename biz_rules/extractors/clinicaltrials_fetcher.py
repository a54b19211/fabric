"""
ClinicalTrials.gov API 数据拉取器
符合项目约定的 api_fetcher 接口
"""

import requests
from typing import Iterator, Dict
from loguru import logger


def fetch_clinicaltrials(api_config: Dict) -> Iterator[Dict]:
    """
    从 ClinicalTrials.gov API 拉取临床试验数据

    参数:
        api_config: 站点配置中的 api 字段，包含:
            - base_url: API 端点
            - params: 请求参数（query.term, pageSize, format 等）
            - max_pages: 最大翻页数（可选，默认 10）

    Yields:
        单条临床试验记录的原始 dict
    """
    base_url = api_config.get("base_url", "https://clinicaltrials.gov/api/v2/studies")
    params = api_config.get("params", {"format": "json"})
    max_pages = api_config.get("max_pages", 10)

    page_token = None
    page_count = 0
    total_records = 0

    logger.info(f"[ClinicalTrials] 开始拉取数据, params={params}")

    while page_count < max_pages:
        current_params = params.copy()
        if page_token:
            current_params["pageToken"] = page_token

        logger.debug(f"[ClinicalTrials] 请求参数: {current_params}")

        try:
            response = requests.get(base_url, params=current_params, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"[ClinicalTrials] API 请求失败: {e}")
            break

        data = response.json()
        studies = data.get("studies", [])

        if not studies:
            logger.info("[ClinicalTrials] 没有更多数据，停止拉取")
            break

        for study in studies:
            total_records += 1
            yield study

        logger.info(f"[ClinicalTrials] 已拉取 {total_records} 条记录")

        # 检查下一页
        page_token = data.get("nextPageToken")
        if not page_token:
            logger.info("[ClinicalTrials] 无下一页 token，停止拉取")
            break

        page_count += 1

    logger.info(f"[ClinicalTrials] 拉取完成，共 {total_records} 条记录")
