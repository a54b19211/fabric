"""SEC EDGAR 的 8-K（临时公告）：和定期报告同一个读取函数（fetch/sites/sec_filings.py），另取附件 EX-99（新闻稿）。"""
from fetch.sites.sec_filings import iter_sec_filings

SITE_KEY = "sec_8k"

CONFIG = {
    "name": "SEC EDGAR 临时公告（8-K）",
    "source_type": "api",
    "url_pattern": "sec.gov",
    "by_group": True,
    "api": {"site": SITE_KEY, "forms": ["8-K"], "since": "2026-01-01", "exhibits": True, "per_company": 5},
    "api_fetcher": iter_sec_filings,
}
