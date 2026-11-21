import re

from clean.headings import relevel
from fetch.extractors.nmpa import continuation_links, extract_nmpa_continuation, extract_nmpa_detail

SITE_KEY = "nmpa"

_ARTICLE = re.compile(r"/(\d{12,})(?:_\d+)?\.html$")


def article_key(url: str) -> str | None:
    """一篇文章的编号：详情页文件名里的时间戳（20261112141319195）。同一篇挂在几个栏目下时网址不同、编号相同。"""
    found = _ARTICLE.search(url or "")
    return found.group(1) if found else None


# 采哪些栏目（2026-11-21 主持人定：只采 /data/docs/NMPA栏目调研.md 的“必采”“可采”，药品子栏，历史到 2014 年）。
# 入口用站点“药品”频道（/yaopin/yp*）的列表：它只列药品的文章，链接直接指向 /xxgk/ 下的正式地址，
# 记录号就是那个正式地址（一篇一条，见 article_key 和 fetch/site_crawler.py 的 duplicate_article）。
# 2026-11-21 用采集同款浏览器实测：/yaopin/ypggtg、/yaopin/ypzhcjd 列表里的链接全是 ../../xxgk/...，
# /yaopin/ypggtg/<编号>.html 这种旧地址不再出现；/xxgk/ggtg/ypggtg/index.html、/xxgk/zhqyj/zhqyjyp/index.html、
# /xxgk/zhcjd/zhcjdyp/index.html 是 404，没有单独的列表，只能在混合列表里按路径筛。
# list_filters：入口 → 只读路径匹配的详情页（其余记 outside_column 跳过，不打开）。
_YP = "https://www.nmpa.gov.cn/yaopin"
_XX = "https://www.nmpa.gov.cn/xxgk"
LIST_FILTERS = {
    # 2021 年 5–6 月前后的条目，列表里有的链到 /yaopin/ypggtg/<编号>.html（同一篇的镜像地址）：照收，
    # 已用 /xxgk/ 地址存过的按编号判重跳过（duplicate_article）
    _YP + "/ypggtg/index.html": r"/xxgk/ggtg/ypggtg/|/yaopin/ypggtg/\d",  # 药品公告通告（站点无“共N页”，约 80 页）
    _YP + "/ypfgwj/index.html": r"/xxgk/fgwj/",                    # 药品法规文件（75 页，最早 1998 年）
    _XX + "/zhqyj/index.html": r"/xxgk/zhqyj/zhqyjyp/",            # 征求意见，只要药品子栏（29 页 570 条，混器械、化妆品）
    _YP + "/ypzhcjd/index.html": r"/xxgk/zhcjd/zhcjdyp/|/yaopin/ypzhcjd/\d",  # 药品政策解读；图解 tjzhc、药你知道 shpjd 不采
    _XX + "/fxjzh/ypfxjch/index.html": r"/xxgk/fxjzh/ypfxjch/",    # 药品飞行检查（8 页 148 条，2014-10 至 2019-01）
    "https://www.nmpa.gov.cn/zwfw/zwfwgggs/zypzbhslgs/index.html": r"/zwfw/zwfwgggs/zypzbhslgs/",  # 中药品种保护受理公示
}

CONFIG = {
    # ── 爬虫参数 ──
    "name": "国家药品监督管理局",
    "url_pattern": "nmpa.gov.cn",
    "start_urls": list(LIST_FILTERS),
    "list_filters": LIST_FILTERS,
    "article_key": article_key,
    # 列表按发布日期从新到旧；一页里出现 2014 年以前的条目就不再往后翻（法规文件列表一直到 1998 年）
    "history_from": "2014-01-01",
    "max_pages": 1000,  # 药品公告通告列表很长：2026-11-21 实测第 120 页 2019-09、第 200 页 2019-01

    # ── 列表页解析 ──
    "wait_selector": ".yp-child-list .list ul li",
    "list_item_selector": ".yp-child-list .list ul li a",
    "list_row_selector": ".yp-child-list .list ul li",  # 一行一条：链接 + 日期（history_from 按它停）

    # ── 翻页配置 ──
    "pagination": {
        "type": "url_pattern",
        "first_page": "index.html",
        "other_pages": "index_{n}.html",
        "last_page_404": True,  # 翻过最后一页站点回 404：列表到头了，不是读取失败
    },

    # ── 详情页解析 ──
    "detail_extractor": extract_nmpa_detail,
    "continuation_links": continuation_links,  # 多页文章的子页 <编号>_N.html
    "continuation_extractor": extract_nmpa_continuation,
    # 整篇（多页文章拼好以后）的小标题按编号样式统一定级（clean/headings.py · relevel）
    "markdown_finish": relevel,
    "detail_wait_selector": None,
    "attachments_inherit_parent": True,  # 附件记录带所属公告的标题、日期、文号
    "single_clean_text": True,  # 正文只在清洗段产出一份，投影不放提取器原样输出
}
