"""
站点配置中心 —— 所有站点的爬虫参数 + 解析配置统一在此维护。

新增站点步骤：
  1. 在 biz_rules/css_extractors.py 里写特解函数（可选，没有则走通解）
  2. 在 SITE_PROFILES 里加一条配置
"""

from biz_rules.css_extractors import extract_nmpa_detail

# httpx 下载附件时使用的请求头（不用于 StealthyFetcher）
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SITE_PROFILES = {
    # -------- 国家药监局 (NMPA) --------
    "nmpa": {
        # ── 爬虫参数 ──
        "name": "国家药品监督管理局",
        "url_pattern": "nmpa.gov.cn",          # URL 匹配标识，router 用来识别站点
        "start_urls": [
            "https://www.nmpa.gov.cn/xxgk/ggtg/index.html",
        ],
        "request_delay_seconds": 2.0,
        "max_pages": 50,

        # ── 列表页解析 ──
        "wait_selector": ".yp-child-list .list ul li",   # 等待该元素出现后再取 HTML
        "list_item_selector": ".yp-child-list .list ul li a",  # 提取详情页链接

        # ── 翻页配置 ──
        # type "url_pattern": URL 有规律，直接批量生成所有列表页
        #   index.html → index_1.html → index_2.html ...
        # type "css_next_btn": 找页面上"下一页"按钮跟随 href
        "pagination": {
            "type": "url_pattern",
            "first_page": "index.html",
            "other_pages": "index_{n}.html",   # {n} 从 1 开始
        },

        # ── 详情页解析 ──
        "detail_extractor": extract_nmpa_detail,  # None 则走 Trafilatura+Qwen 通解
        "detail_wait_selector": None,
    },

    # -------- CDE 药品审评中心 --------
    "cde": {
        # ── 爬虫参数 ──
        "name": "国家药品审评中心",
        "url_pattern": "cde.org.cn",
        "start_urls": [
            "https://www.cde.org.cn/main/news/listpage/545b4c0545e88c8f3ca727c0b55f63e1",
        ],
        "request_delay_seconds": 3.0,
        "max_pages": 30,

        # ── 列表页解析（待确认页面结构后填入）──
        "wait_selector": None,
        "list_item_selector": None,

        # ── 翻页配置 ──
        "pagination": {
            "type": "css_next_btn",
            "selector": "a.next, a.page-next",
        },

        # ── 详情页解析（暂无特解，走通解）──
        "detail_extractor": None,
        "detail_wait_selector": None,
    },
}

# ======================== 本地 PDF 扫描目录 ========================
LOCAL_PDF_SCAN_DIRS = [
    # 例如: r"D:\历史文献\药学期刊",
]
