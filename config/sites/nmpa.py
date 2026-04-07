from biz_rules.extractors.nmpa import extract_nmpa_detail

SITE_KEY = "nmpa"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "国家药品监督管理局",
    "url_pattern": "nmpa.gov.cn",
    "start_urls": [
        "https://www.nmpa.gov.cn/xxgk/ggtg/index.html",
    ],
    "request_delay_seconds": 2.0,
    "max_pages": 50,

    # ── 列表页解析 ──
    "wait_selector": ".yp-child-list .list ul li",
    "list_item_selector": ".yp-child-list .list ul li a",

    # ── 翻页配置 ──
    "pagination": {
        "type": "url_pattern",
        "first_page": "index.html",
        "other_pages": "index_{n}.html",
    },

    # ── 详情页解析 ──
    "detail_extractor": extract_nmpa_detail,
    "detail_wait_selector": None,
}
