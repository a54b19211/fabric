from biz_rules.extractors.fda.press import extract_fda_press

SITE_KEY = "fda_press"

CONFIG = {
    "name": "FDA Press Announcements",
    "url_pattern": "fda.gov/news-events/press-announcements",
    "start_urls": ["https://www.fda.gov/news-events/press-announcements"],
    "request_delay_seconds": 2.0,
    "max_pages": 20,
    "disable_resources": False,  # FDA 使用 Cloudflare，需加载完整资源才能通过检测
    "solve_cloudflare": True,
    "wait_selector": "h3.lcds-text-heading",
    "list_item_selector": "h3.lcds-text-heading a",
    "pagination": {
        "type": "url_pattern",
        "subtype": "query_param",
        "param": "page",
        "start": 0,
    },
    "detail_extractor": extract_fda_press,
    "detail_wait_selector": "h1",
}
