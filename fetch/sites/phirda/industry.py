"""产业资讯 (news_9)"""
from fetch.extractors.phirda import extract_phirda_detail

SITE_KEY = "phirda_industry"

CONFIG = {
    "name": "中国药促会·产业资讯",
    "url_pattern": "phirda.com",
    "start_urls": ["https://www.phirda.com/news_9.html"],
    "max_pages": 50,
    "wait_selector": None,
    "list_item_selector": "a[href*='artilce_']",
    "use_simple_fetch": True,
    "disable_resources": True,
    "pagination": {
        "type": "url_pattern",
        "first_page": "news_9.html",
        "other_pages": "news_9/p_{n}.html",
    },
    "detail_extractor": extract_phirda_detail,
    "detail_wait_selector": None,
}
