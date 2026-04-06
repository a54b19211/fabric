SITE_KEY = "cde"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "国家药品审评中心",
    "url_pattern": "cde.org.cn",
    "start_urls": [
        "https://www.cde.org.cn/main/news/listpage/545b4c0545e88c8f3ca727c0b55f63e1",
    ],
    "request_delay_seconds": 3.0,
    "max_pages": 30,

    # ── 列表页解析（待确认页面结构后填入）──
    "wait_selector": None,
    "list_item_selector": None,

    # ── 翻页配置 ──
    "pagination": {
        "type": "css_next_btn",
        "selector": "a.next, a.page-next",
    },

    # ── 详情页解析（暂无特解，走通解）──
    "detail_extractor": None,
    "detail_wait_selector": None,
}
