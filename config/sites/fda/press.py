from biz_rules.extractors.fda.press import extract_fda_press

SITE_KEY = "fda_press"

CONFIG = {
    "name": "FDA Press Announcements",
    "url_pattern": "fda.gov/news-events/press-announcements",
    # 列表页已搬到 fda-newsroom 下（旧地址 /news-events/press-announcements?page=0 现在是 404）；
    # 新闻稿详情页的地址还是 /news-events/press-announcements/<名字>，url_pattern 不用改。
    "start_urls": ["https://www.fda.gov/news-events/fda-newsroom/press-announcements"],
    "max_pages": 20,
    # 列表页和详情页都是静态 HTML：用简单请求读（1-2 秒一页），不开浏览器。浏览器一页要几十秒，对方连接被重置、
    # 卡住时一次读取 130 秒一条都读不到（见 fda_drug_safety）。简单请求不需要加载资源，也不需要过 Cloudflare 验证。
    "use_simple_fetch": True,
    # 列表按月分组：div.view-content 里每条是 li > a（链接文字是“日期 - 标题”）
    "wait_selector": "div.view-content li a",
    "list_item_selector": "div.view-content li a",
    # 服务器分页，?page=0、?page=1 …（页码 0 到 38）
    "pagination": {
        "type": "url_pattern",
        "subtype": "query_param",
        "param": "page",
        "start": 0,
    },
    "detail_extractor": extract_fda_press,
    "detail_wait_selector": "h1",
}
