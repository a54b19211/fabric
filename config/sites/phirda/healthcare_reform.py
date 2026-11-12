"""医改专题 (news_12)"""
from biz_rules.extractors.phirda import extract_phirda_detail

SITE_KEY = "phirda_healthcare_reform"

CONFIG = {
    "name": "中国药促会·医改专题",
    "url_pattern": "phirda.com",
    "start_urls": ["https://www.phirda.com/news_12.html"],
    "max_pages": 50,
    "wait_selector": None,
    "list_item_selector": "a[href*='artilce_']",
    "use_simple_fetch": True,
    "disable_resources": True,
    "pagination": {
        "type": "url_pattern",
        "first_page": "news_12.html",
        "other_pages": "news_12/p_{n}.html",
    },
    "detail_extractor": extract_phirda_detail,
    "detail_wait_selector": None,
}
