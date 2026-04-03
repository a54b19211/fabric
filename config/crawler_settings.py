"""
爬虫参数配置 —— 目标站点 URL、请求头、延时等。
新增站点时只需要在 SITE_PROFILES 字典中追加条目即可。
"""

# ======================== 通用请求头 ========================
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ======================== 站点画像字典 ========================
# 每个 key 是站点的简短标识符，router 会根据 URL 匹配对应 profile
SITE_PROFILES = {
    # -------- 国家药监局 (NMPA) --------
    "nmpa": {
        "name": "国家药品监督管理局",
        "url_pattern": "nmpa.gov.cn",
        "start_urls": [
            "https://www.nmpa.gov.cn/xxgk/ggtg/index.html",
        ],
        "use_css_extractor": True,
        "request_delay_seconds": 2.0,
        "wait_selector": ".yp-child-list .list ul li",
        "max_pages": 50,
        # 列表页条目选择器：提取详情页链接
        "list_item_selector": ".yp-child-list .list ul li a",
        # 翻页配置
        # type "url_pattern": 按 URL 规律拼接翻页，index.html → index_1.html → index_2.html
        # type "css_next_btn": 找页面上"下一页"按钮的 href
        "pagination": {
            "type": "url_pattern",
            "first_page": "index.html",
            "other_pages": "index_{n}.html",  # {n} 从 1 开始
        },
    },

    # -------- 示例：CDE 药品审评中心 --------
    "cde": {
        "name": "国家药品审评中心",
        "url_pattern": "cde.org.cn",
        "start_urls": [
            "https://www.cde.org.cn/main/news/listpage/"
            "545b4c0545e88c8f3ca727c0b55f63e1",
        ],
        "use_css_extractor": False,
        "request_delay_seconds": 3.0,
        "wait_selector": None,
        "max_pages": 30,
        "list_item_selector": None,  # 待确认后填入
        # CDE 翻页方式待确认，暂用 css_next_btn 占位
        "pagination": {
            "type": "css_next_btn",
            "selector": "a.next, a.page-next",
        },
    },
}

# ======================== 本地 PDF 扫描目录 ========================
LOCAL_PDF_SCAN_DIRS = [
    # 在此添加需要批量扫描的本地 PDF 目录路径
    # 例如: r"D:\历史文献\药学期刊",
]
