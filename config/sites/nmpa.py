from biz_rules.extractors.nmpa import extract_nmpa_detail

SITE_KEY = "nmpa"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "国家药品监督管理局",
    "url_pattern": "nmpa.gov.cn",
    "start_urls": [
        "https://www.nmpa.gov.cn/xxgk/ggtg/index.html",
    ],
    "request_delay_seconds": 1.0,   # 降低延迟，nmpa 反爬不强
    "max_pages": 50,

    # ── 列表页解析 ──
    "wait_selector": ".yp-child-list .list ul li",
    "list_item_selector": ".yp-child-list .list ul li a",

    # ── 翻页配置 ──
    "pagination": {
        "type": "url_pattern",
        "first_page": "index.html",
        "other_pages": "index_{n}.html",
    },

    # ── 详情页解析 ──
    "detail_extractor": extract_nmpa_detail,
    "detail_wait_selector": None,

    # ── 附件策略 ──
    # True：附件内容合并进父文档 raw_content，不单独入库
    # False（默认）：附件作为独立 document 入库，parent_uuid 指向父文档
    "merge_attachments": True,
}
