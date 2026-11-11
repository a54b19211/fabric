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
        # 截止日，格式 YYYYMMDD；留空 = 读取当天。接口要求 to 必须有值：不传或传空串不报错，但一条都查不到（实测）；
        # 写成将来的日期没问题。以前写死成 20260410，之后发布的公告全读不到。
        "to_date": "",
        # 接口一次最多返回最新的 100 条（页面上有“更多”按钮，这里没接）。公告多的公司（如 00013 和黄医药，2024 年至今共 239 条）
        # 只读到最新 100 条；要读更早的，把 from_date 往后调，分段读。
        # 不是公告的条目：文件类型栏为“展示文件”的行，内容只是一句“发行人刚在英文版发布了公告，中文版本或会/或不会在本版面发布”，
        # 各公司指向同一个网址、没有任何信息，直接跳过。
        "skip_headlines": ["展示文件"],
        "request_delay": 3.0,
        "companies": PHARMA_COMPANIES,
    },

    "uid_field": "pdf_url",
    "field_map": {
        "title": "headline",
        "publish_date": "release_time",
        "summary": "headline",
        "source_url": "pdf_url",
        "stock_code": "stock_code",
        "stock_name": "stock_name",
    }
}
