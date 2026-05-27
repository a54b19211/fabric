from biz_rules.extractors.cde import extract_cde_detail

SITE_KEY = "cde_zdyz"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "药品审评中心-指导原则",
    "url_pattern": "cde.org.cn",
    "start_urls": [
        "https://www.cde.org.cn/zdyz/listpage/9cd8db3b7530c6fa0c86485e563f93c7",
    ],
    "request_delay_seconds": 2.0,
    "max_pages": 100,

    # ── 列表页解析 ──
    "wait_selector": "div.news_item",
    "list_item_selector": "div.news_item a[href*='viewInfoCommon']",

    # ── 翻页配置 ──
    "pagination": {
        "type": "js_click",
        "next_btn_selector": "a.layui-laypage-next:not(.layui-disabled)",
    },

    # ── 详情页解析 ──
    "merge_attachments": True,
    "detail_extractor": extract_cde_detail,
    "detail_wait_selector": "div.news_detail_box",
}
