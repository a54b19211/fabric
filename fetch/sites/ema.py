"""EMA（欧洲药品管理局）新闻与新闻稿：官方 RSS（正文全文），用通用 RSS 读取（fetch/sites/rss_press.py）。
订阅源一次只给最新 5 条，靠定时刷新慢慢攒；更老的和药品审评文件（EPAR）没接。"""
from fetch.sites.rss_press import iter_rss

SITE_KEY = "ema"

CONFIG = {
    "name": "EMA News",
    "source_type": "api",
    "url_pattern": "ema.europa.eu",
    "api": {"site": SITE_KEY, "category": "监管新闻",
            "feeds": [{"name": "EMA News and press releases", "url": "https://www.ema.europa.eu/en/news.xml"}]},
    "api_fetcher": iter_rss,
}
