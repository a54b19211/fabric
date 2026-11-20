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
    # 新闻中心的条目链到 viewInfoCommon；政策法规的条目链到 /main/policy/view/…（还有 regulatview、zcjdview），
    # 有的条目直接链到药监局、市场监管总局的网站，不是本站页面，不取
    "list_item_selector": "div.news_item a[href*='viewInfoCommon'], div.news_item a.right_default[href^='/main/policy/']",

    # ── 翻页配置 ──
    "pagination": {
        "type": "js_click",
        "next_btn_selector": "a.layui-laypage-next:not(.layui-disabled)",
    },

    # ── 详情页解析 ──
    # 详情页先回一个 202 的脚本挑战页，要等正文区出来再读。等的就是提取函数读的正文区（新版 / 旧版两种写法），
    # 不等外面的框（原采集层 11-13 修正；2026-11-14 实测新版页正文区和框同时出现，读到的一样）。
    "detail_extractor": extract_cde_detail,
    "detail_wait_selector": "div.new_detail_content, div.new_detail_content_old",
}
