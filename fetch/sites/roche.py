"""Roche Media Releases 站点配置（API 型，StealthyFetcher 采集）。"""
from fetch.extractors.roche.roche_web_fetcher import iter_roche_web

SITE_KEY = "roche"

CONFIG = {
    "name": "Roche Media Releases",
    "source_type": "api",
    "url_pattern": "roche.com/media/releases",
    "api": {
        "start_url": "https://www.roche.com/media/releases",
        "max_articles": 0,
        "year_start": 0,  # 从哪一年往前读；0（或不写）= 今年
        "year_end": 2016,
        "max_pages_per_year": 100,
    },
    "api_fetcher": iter_roche_web,
    "uid_field": "",  # iter_roche_web 内部已生成 UUID
    "field_map": {},
}
