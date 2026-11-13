from fetch.extractors.cde import extract_cde_detail

SITE_KEY = "cde"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "国家药品审评中心",
    "url_pattern": "cde.org.cn",
    "start_urls": [
        "https://www.cde.org.cn/main/news/listpage/545cf855a50574699b46b26bcb165f32",     # 新闻中心
        "https://www.cde.org.cn/main/policy/listpage/9f9c74c73e0f8f56a8bfbc646055026d",   # 政策法规
    ],
    "max_pages": 200,

    # ── 列表页解析 ──
    "wait_selector": "div.news_item",
    "list_item_selector": "div.news_item a[href*='viewInfoCommon']",

    # ── 翻页配置 ──
    "pagination": {
        "type": "js_click",
        "next_btn_selector": "a.layui-laypage-next:not(.layui-disabled)",
    },

    # ── 详情页解析 ──
    "detail_extractor": extract_cde_detail,
    "detail_wait_selector": "div.news_detail_box",
}
