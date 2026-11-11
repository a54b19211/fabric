from biz_rules.extractors.fda.press import extract_fda_press

SITE_KEY = "fda_press"

CONFIG = {
    "name": "FDA Press Announcements",
    "url_pattern": "fda.gov/news-events/press-announcements",
    # 列表页已搬到 fda-newsroom 下（旧地址 /news-events/press-announcements?page=0 现在是 404）；
    # 新闻稿详情页的地址还是 /news-events/press-announcements/<名字>，url_pattern 不用改。
    "start_urls": ["https://www.fda.gov/news-events/fda-newsroom/press-announcements"],
    "request_delay_seconds": 2.0,
    "max_pages": 20,
    "disable_resources": False,  # FDA 使用 Cloudflare，需加载完整资源才能通过检测
    "solve_cloudflare": True,
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
