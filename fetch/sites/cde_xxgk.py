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
    # 只读前 10 页。表格页没有详情链接，续读要从第 1 页点“下一页”重放到续读位置，一页约 2 秒：
    # 原来 200 页，续读位置到了第 59 页后，每次 120 秒都在重放途中用完，0 条、位置不动，一直卡住。
    # 表格按页号存，新数据进来整体后移，深处的页本来也对不上同一批记录，只读最新几页。
    "max_pages": 10,

    # ── 表格直提配置 ──
    "wait_selector": "table:not(.detail_table) tbody tr",

    # ── 翻页配置 ──
    "pagination": {
        "type": "js_click",
        "next_btn_selector": "a.layui-laypage-next:not(.layui-disabled)",
    },
}
