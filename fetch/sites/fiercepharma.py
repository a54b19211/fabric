"""Fierce Pharma 站点配置（RSS + Playwright 详情页混合采集）。"""
from fetch.extractors.fiercepharma.fiercepharma_fetcher import iter_fiercepharma

SITE_KEY = "fiercepharma"

CONFIG = {
    "name": "Fierce Pharma",
    "source_type": "api",
    "url_pattern": "fiercepharma.com",
    "api": {
        # 总源 /rss/xml 已去掉：每次回 403（Cloudflare 人机验证，cf-mitigated: challenge），没有替代地址
        # （/rss.xml 只有 10 条，多数是网络研讨会、资源页），不绕过验证。2026-11-12 查证。
        "rss_feeds": [
            "https://www.fiercepharma.com/rss/Pharma/xml",
            "https://www.fiercepharma.com/rss/manufacturing/xml",
        ],
        "max_articles": 100,
    },
    "api_fetcher": iter_fiercepharma,
    "uid_field": "",   # iter_fiercepharma 内部已生成 UUID，field_map 不再使用
    "field_map": {},
}
