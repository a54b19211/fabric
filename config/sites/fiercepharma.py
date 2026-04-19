"""Fierce Pharma 站点配置（RSS + Playwright 详情页混合采集）。"""
from biz_rules.extractors.fiercepharma.fiercepharma_fetcher import iter_fiercepharma
from biz_rules.cleaners.fiercepharma import clean_fiercepharma

SITE_KEY = "fiercepharma"

CONFIG = {
    "name": "Fierce Pharma",
    "source_type": "api",
    "url_pattern": "fiercepharma.com",
    "api": {
        "rss_feeds": [
            "https://www.fiercepharma.com/rss/xml",
            "https://www.fiercepharma.com/rss/Pharma/xml",
            "https://www.fiercepharma.com/rss/manufacturing/xml",
        ],
        "max_articles": 100,
    },
    "api_fetcher": iter_fiercepharma,
    "uid_field": "",   # iter_fiercepharma 内部已生成 UUID，field_map 不再使用
    "field_map": {},
    "content_cleaner": clean_fiercepharma,
}
