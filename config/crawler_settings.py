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
        "url_pattern": "nmpa.gov.cn",               # URL 中包含此字符串即匹配
        "start_urls": [
            "https://www.nmpa.gov.cn/xxgk/ggtg/index.html",
        ],
        "use_css_extractor": True,                    # 走 Fast Track 硬解
        "request_delay_seconds": 2.0,                 # 请求间隔（秒），礼貌爬取
        "headers": DEFAULT_HEADERS,
        "max_pages": 50,                              # 单次运行最大翻页数
    },

    # -------- 示例：CDE 药品审评中心 --------
    "cde": {
        "name": "国家药品审评中心",
        "url_pattern": "cde.org.cn",
        "start_urls": [
            "https://www.cde.org.cn/main/news/listpage/"
            "545b4c0545e88c8f3ca727c0b55f63e1",
        ],
        "use_css_extractor": False,                   # 无专属硬解，走 Slow Track
        "request_delay_seconds": 3.0,
        "headers": DEFAULT_HEADERS,
        "max_pages": 30,
    },
}

# ======================== 本地 PDF 扫描目录 ========================
LOCAL_PDF_SCAN_DIRS = [
    # 在此添加需要批量扫描的本地 PDF 目录路径
    # 例如: r"D:\历史文献\药学期刊",
]
