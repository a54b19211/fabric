"""
爬虫配置加载器。

新增站点：在 config/sites/ 下新建一个 .py 文件，声明 SITE_KEY 和 CONFIG 即可。
无需修改本文件，不会产生合并冲突。

站点文件模板：
    SITE_KEY = "my_site"
    CONFIG = {
        "name": "站点名称",
        "url_pattern": "example.com",
        "start_urls": ["https://www.example.com/news/index.html"],
        "request_delay_seconds": 2.0,
        "max_pages": 10,
        "wait_selector": None,
        "list_item_selector": None,
        "pagination": {"type": "url_pattern", "first_page": "index.html", "other_pages": "index_{n}.html"},
        "detail_extractor": None,   # None 走通解，或传入 css_extractors 里的函数
        "detail_wait_selector": None,
    }
"""

import importlib
from pathlib import Path

# httpx 下载附件时使用的请求头（不用于 StealthyFetcher）
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 递归扫描 config/sites/ 下所有 .py 文件（含子目录），加载 SITE_KEY + CONFIG
SITE_PROFILES: dict = {}

_sites_dir = Path(__file__).parent / "sites"
for _py_file in sorted(_sites_dir.rglob("*.py")):
    if _py_file.name.startswith("_"):
        continue
    # 将文件路径转成模块路径，如 config/sites/fda/press.py → config.sites.fda.press
    _rel = _py_file.relative_to(Path(__file__).parent.parent)
    _mod_path = ".".join(_rel.with_suffix("").parts)
    _mod = importlib.import_module(_mod_path)
    if hasattr(_mod, "SITE_KEY") and hasattr(_mod, "CONFIG"):
        SITE_PROFILES[_mod.SITE_KEY] = _mod.CONFIG

# ======================== 本地 PDF 扫描目录 ========================
LOCAL_PDF_SCAN_DIRS = [

]
