SITE_KEY = "cde_xxgk"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "国家药品审评中心-信息公开",
    "url_pattern": "cde.org.cn",
    "source_type": "table_page",
    "start_urls": [
        "https://www.cde.org.cn/main/xxgk/listpage/9f9c74c73e0f8f56a8bfbc646055026d",     # 信息公开
        "https://www.cde.org.cn/main/xxgk/listpage/4b5255eb0a84820cef4ca3e8b6bbe20c",     # 临床试验默示许可
    ],
    "request_delay_seconds": 2.0,
    "max_pages": 200,

    # ── 表格直提配置 ──
    "wait_selector": "table:not(.detail_table) tbody tr",

    # ── 翻页配置 ──
    "pagination": {
        "type": "js_click",
        "next_btn_selector": "a.layui-laypage-next:not(.layui-disabled)",
    },
}
