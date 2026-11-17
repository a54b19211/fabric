"""medRxiv 预印本：和 bioRxiv 同一个官方接口、同一个读取函数（fetch/sites/biorxiv.py），只是 server 不同。"""
from fetch.sites.biorxiv import iter_openrxiv

SITE_KEY = "medrxiv"

CONFIG = {
    "name": "medRxiv",
    "source_type": "api",
    "url_pattern": "medrxiv.org",
    "api": {"server": "medrxiv", "days": 14},
    "api_fetcher": iter_openrxiv,
}
