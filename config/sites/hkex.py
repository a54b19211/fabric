"""
港交所披露易站点配置
"""

from biz_rules.extractors.hkex_fetcher import fetch_hkex_announcements
from config.sites.hkex_pharma_list_auto import PHARMA_COMPANIES
SITE_KEY = "hkex"

CONFIG = {
    "name": "港交所披露易",
    "source_type": "api",
    "api_fetcher": fetch_hkex_announcements,

    "api": {
    "base_url": "https://www1.hkexnews.hk/search/titlesearch.xhtml",
    "lang": "ZH",
    "market": "SEHK",
    "searchType": 0,
    "from_date": "20240101",
    "to_date": "20260410",
    "request_delay": 3.0,
    "companies": PHARMA_COMPANIES,  # 使用列表
    },

    "uid_field": "stock_code",
    "field_map": {
    "title": "headline",
    "publish_date": "release_time",
    "summary": "headline",
    "source_url": "pdf_url",
    "stock_code": "stock_code",  # 添加这行
    "stock_name": "stock_name",  # 添加这行
    }
}
