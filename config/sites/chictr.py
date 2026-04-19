from biz_rules.extractors.chictr.chictr import extract_chictr_detail
from db.sites.chictr import db_writer as chictr_db_writer
from biz_rules.cleaners.clinical_trial import clean_clinical_trial

SITE_KEY = "chictr"

CONFIG = {
    # ── 基本信息 ──
    "name": "中国临床试验注册中心",
    "url_pattern": "chictr.org.cn/showproj",
    "start_urls": [
        "https://www.chictr.org.cn/searchproj.html",
    ],
    "request_delay_seconds": 2.0,
    "max_pages": 50,          # 按需调整，全量约 12000 页

    # ── 列表页解析 ──
    # 等待 tbody 里出现数据行
    "wait_selector": "tbody tr td a[href*='showproj']",
    "list_item_selector": "tbody tr td a[href*='showproj']",

    # ── 翻页配置 ──
    # 翻页 URL 格式：searchproj.html?page=1&title=&...
    # 用 query_param 子模式，参数名 page，起始值 1
    "pagination": {
        "type": "url_pattern",
        "subtype": "query_param",
        "param": "page",
        "start": 1,
    },

    # ── 详情页解析 ──
    "detail_extractor": extract_chictr_detail,
    "db_writer": chictr_db_writer,
    "detail_wait_selector": "table td.left_title",

    # 详情页用 simple_fetch 即可（服务端渲染，无需 Playwright）
    "use_simple_fetch": False,
    "disable_resources": True,
    "solve_cloudflare": False,
    "content_cleaner": clean_clinical_trial,
}
