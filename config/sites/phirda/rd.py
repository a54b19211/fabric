"""研发追踪 (news_11)"""
from biz_rules.extractors.phirda import extract_phirda_detail

SITE_KEY = "phirda_rd"

CONFIG = {
    "name": "中国药促会·研发追踪",
    "url_pattern": "phirda.com",
    "start_urls": ["https://www.phirda.com/news_11.html"],
    "request_delay_seconds": 1.5,
    "max_pages": 50,
    "wait_selector": None,
    "list_item_selector": "a[href*='artilce_']",
    "use_simple_fetch": True,
    "disable_resources": True,
    "pagination": {
        "type": "url_pattern",
        "first_page": "news_11.html",
        "other_pages": "news_11/p_{n}.html",
    },
    "detail_extractor": extract_phirda_detail,
    "detail_wait_selector": None,
}
