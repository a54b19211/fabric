"""Roche Media Releases 站点配置（官方 RSS 采集）。

数据源：https://www.roche.com/med_news_xml.xml
采集方式：RSS XML，无需 JS 渲染，正文已内嵌在 <description> 中。
"""
from biz_rules.extractors.roche import iter_roche

SITE_KEY = "roche"

CONFIG = {
    "name": "Roche Media Releases",
    "source_type": "api",
    "url_pattern": "roche.com",
    "api": {
        "rss_url": "https://www.roche.com/med_news_xml.xml",
        "max_articles": 0,       # 0 = 全量；调试时可改为 10
        "request_delay": 1.0,    # 礼貌延迟（秒）
    },
    "api_fetcher": iter_roche,
    "uid_field": "",   # iter_roche 内部已生成 UUID
    "field_map": {},   # iter_roche 已返回标准格式，无需字段映射
}
