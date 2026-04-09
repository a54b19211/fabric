"""
通用 API 采集器
支持分页、参数配置、数据转换
"""

import json
import requests
from typing import Dict, List, Any, Optional
from pathlib import Path
from loguru import logger
from datetime import datetime

from config.settings import APP
from config.crawler_settings import SITE_PROFILES


class APICollector:
    """通用 API 数据采集器"""

    def __init__(self, site_key: str):
        self.site_key = site_key
        self.config = SITE_PROFILES.get(site_key, {})
        self.api_config = self.config.get("api_config", {})

        if not self.api_config:
            raise ValueError(f"站点 {site_key} 没有 api_config 配置")

    def collect(self, query_params: Optional[Dict] = None) -> List[Dict]:
        """执行 API 采集"""
        all_data = []

        # 合并默认参数和自定义参数
        params = self.api_config.get("params", {}).copy()
        if query_params:
            params.update(query_params)

        params = {k: v for k, v in params.items() if v is not None} #过滤None值

        base_url = self.api_config["base_url"]
        page_token = None
        page_count = 0
        max_pages = self.config.get("max_pages", 10)

        while page_count < max_pages:
            # 添加分页参数
            if page_token:
                pagination = self.api_config.get("pagination", {})
                param_name = pagination.get("param_name", "pageToken")
                params[param_name] = page_token

            logger.info(f"请求 API: {base_url}")
            logger.debug(f"参数: {params}")

            try:
                response = requests.get(
                    base_url,
                    params=params,
                    timeout=self.config.get("timeout", 30)
                )

                if response.status_code != 200:
                    logger.error(f"API 请求失败: {response.status_code}")
                    logger.error(f"响应内容: {response.text[:500]}")
                    break

                data = response.json()
                studies = data.get("studies", [])

                if not studies:
                    break

                # 转换数据格式
                converted = self._convert_data(studies)
                all_data.extend(converted)

                logger.info(f"已获取 {len(all_data)} 条记录")

                # 获取下一页 token
                pagination = self.api_config.get("pagination", {})
                token_field = pagination.get("response_field", "nextPageToken")
                page_token = data.get(token_field)

                if not page_token:
                    break

                page_count += 1

            except Exception as e:
                logger.error(f"采集异常: {e}")
                break

        return all_data

    def _convert_data(self, studies: List[Dict]) -> List[Dict]:
        """将 API 数据转换为框架标准格式"""
        results = []
        field_mapping = self.config.get("field_mapping", {})

        for study in studies:
            try:
                # 提取字段
                nct_id = self._get_nested_value(study, field_mapping.get("uuid", "nctId"))
                if not nct_id:
                    continue

                title = self._get_nested_value(study, field_mapping.get("title", "briefTitle"))
                status = self._get_nested_value(study, field_mapping.get("status", "overallStatus"))

                # 构建标准格式
                parsed = {
                    "uuid": nct_id,
                    "source_url": f"https://clinicaltrials.gov/ct2/show/{nct_id}",
                    "source_site": self.site_key,
                    "content_type": "api_json",
                    "title": title or "Untitled",
                    "content": json.dumps(study, indent=2, ensure_ascii=False),
                    "markdown": self._generate_markdown(study, field_mapping),
                    "raw_data": study  # 保留原始数据
                }
                results.append(parsed)

            except Exception as e:
                logger.warning(f"转换数据失败: {e}")
                continue

        return results

    def _get_nested_value(self, data: Dict, path: str, default: Any = "") -> Any:
        """从嵌套字典中获取值，支持点号路径"""
        keys = path.split(".")
        value = data

        for key in keys:
            if isinstance(value, dict):
                value = value.get(key, default)
            else:
                return default

        return value

    def _generate_markdown(self, study: Dict, mapping: Dict) -> str:
        """生成 Markdown 格式的摘要"""
        nct_id = self._get_nested_value(study, mapping.get("uuid", "nctId"))
        title = self._get_nested_value(study, mapping.get("title", "briefTitle"))
        status = self._get_nested_value(study, mapping.get("status", "overallStatus"))
        conditions = self._get_nested_value(study, mapping.get("conditions", "conditions"), [])

        # 处理 conditions 可能是列表的情况
        if isinstance(conditions, list):
            conditions_str = ", ".join([c.get("name", "") if isinstance(c, dict) else str(c) for c in conditions])
        else:
            conditions_str = str(conditions)

        return f"""# {title}

**NCT ID:** {nct_id}
**Status:** {status}
**Conditions:** {conditions_str}

---
*Data fetched from ClinicalTrials.gov API*
"""

    def save_data(self, data: List[Dict]) -> Dict:
        """保存数据到框架目录"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 保存到 json_envelopes
        json_path = APP.JSON_DIR / f"{self.site_key}_{timestamp}.json"
        # 包装成字典格式
        output_data = {
            "records": data,  # 原始数据列表
            "source": self.site_key,
            "timestamp": timestamp,
            "total_count": len(data)
        }

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)

        # 保存到 markdown
        md_path = APP.MARKDOWN_DIR / f"{self.site_key}_{timestamp}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            for item in data:
                f.write(item["markdown"])
                f.write("\n\n---\n\n")

        return {
            "json_path": json_path,
            "md_path": md_path,
            "count": len(data)
        }
