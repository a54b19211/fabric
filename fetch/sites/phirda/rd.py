"""研发追踪 (news_11)"""
from fetch.extractors.phirda import extract_phirda_detail

SITE_KEY = "phirda_rd"

CONFIG = {
    "name": "中国药促会·研发追踪",
    "url_pattern": "phirda.com",
    "start_urls": ["https://www.phirda.com/news_11.html"],
    "max_pages": 50,
    "wait_selector": None,
    # 只取左栏本栏目的文章（div.inLeft 里每条的标题链接）。右栏“热门资讯”每个栏目都一样（多是药促会自己的会议稿），
    # 原来的 a[href*='artilce_'] 把它也当成本栏目，四个栏目各存了 15～20 篇不属于本栏目的文章（2026-11-20 核对）
    "list_item_selector": "div.inLeft div.inC_R > a[href*='artilce_']",
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
