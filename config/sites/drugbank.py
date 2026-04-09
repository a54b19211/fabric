"""DrugBank API 站点配置。

DrugBank 是全球最权威的药物数据库，收录 16,000+ 药物的
化学、药理、适应症、相互作用等信息。

API 文档：https://docs.drugbank.com/v1/
申请 Key：https://dev.drugbank.com/sign_up
"""
import os
from biz_rules.extractors.drugbank import iter_drugbank

SITE_KEY = "drugbank"

CONFIG = {
    "name": "DrugBank 药物数据库",
    "source_type": "api",
    "url_pattern": "drugbank.com",
    "api": {
        # API Key：优先读环境变量 DRUGBANK_API_KEY，也可直接填在这里
        "api_key": os.getenv("DRUGBANK_API_KEY", ""),
        # 区域：us / ca / eu（影响上市状态过滤）
        "region": "us",
        # 关键词搜索，留空拉全量（需要商业授权）
        # 示例：只拉 approved 状态的小分子药物
        "q": "",
        # 最多拉取条数，0 = 全量；调试时建议先设 100
        "max_records": 100,
        # 每页条数，最大 100
        "page_size": 50,
    },
    "api_fetcher": iter_drugbank,
    "uid_field": "drugbank_id",
    "field_map": {
        "title":            "name",
        "publish_date":     "updated",
        "summary":          "description",
        "drug_name":        "name",
        "manufacturer":     "",
        "category":         "__const__:药物数据库",
        "source_url":       "source_url",
    },
}
