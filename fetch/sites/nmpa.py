from fetch.extractors.nmpa import extract_nmpa_detail

SITE_KEY = "nmpa"

CONFIG = {
    # ── 爬虫参数 ──
    "name": "国家药品监督管理局",
    "url_pattern": "nmpa.gov.cn",
    "start_urls": [
        "https://www.nmpa.gov.cn/xxgk/ggtg/index.html",          # 公告通告
        "https://www.nmpa.gov.cn/xxgk/fgwj/index.html",          # 法规文件
        "https://www.nmpa.gov.cn/xxgk/zhcjd/index.html",         # 政策解读
        "https://www.nmpa.gov.cn/xxgk/zhqyj/index.html",         # 征求意见（栏目路径是 zhqyj；原来写的 zhqyi 不存在，会跳回首页）
        "https://www.nmpa.gov.cn/xxgk/fxjzh/index.html",         # 飞行检查
        "https://www.nmpa.gov.cn/yaopin/ypggtg/index.html",      # 药品公告通告
        "https://www.nmpa.gov.cn/yaopin/ypzhcjd/index.html",     # 药品政策解读
    ],
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
}
