from biz_rules.extractors.cde import extract_cde_detail

SITE_KEY = "cde_zdyz"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "药品审评中心-指导原则",
    "url_pattern": "cde.org.cn",
    "start_urls": [
        "https://www.cde.org.cn/zdyz/listpage/9cd8db3b7530c6fa0c86485e563f93c7",
    ],
    "max_pages": 100,

    # ── 列表页解析 ──
    # 列表是一张表，每行一份指导原则，标题就是详情页链接（/zdyz/domesticinfopage?zdyzIdCODE=…）；
    # 页面上还有别的表，所以按链接地址选，不按“第几张表”选。
    "wait_selector": "tbody a[href*='domesticinfopage']",
    "list_item_selector": "tbody a[href*='domesticinfopage']",

    # ── 翻页配置 ──
    "pagination": {
        "type": "js_click",
        "next_btn_selector": "a.layui-laypage-next:not(.layui-disabled)",
    },

    # ── 详情页解析 ──
    # 详情页是一张“名称 / 适用范围 / 版本状态 / 附件”的信息表（table.layui-table），指导原则正文在附件 PDF 里。
    "detail_extractor": extract_cde_detail,
    "detail_wait_selector": "table.layui-table",
}
