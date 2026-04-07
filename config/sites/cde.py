from biz_rules.extractors.cde import extract_cde_detail

SITE_KEY = "cde"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "国家药品审评中心",
    "url_pattern": "cde.org.cn",
    "start_urls": [
        "https://www.cde.org.cn/main/news/listpage/3cc45b396497b598341ce3af000490e5",
    ],
    "request_delay_seconds": 2.0,
    "max_pages": 365,

    # ── 列表页解析 ──
    "wait_selector": "div.news_item",
    "list_item_selector": "div.news_item a[href*='/main/news/viewInfoCommon/']",

    # ── 翻页配置 ──
    "pagination": {
        "type": "js_click",
        "next_btn_selector": "a.layui-laypage-next:not(.layui-disabled)",
    },

    # ── 详情页解析 ──
    "detail_extractor": extract_cde_detail,
    "detail_wait_selector": "div.news_detail_box",
}
