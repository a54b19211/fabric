from fetch.extractors.fda.drug_safety import extract_fda_drug_safety

SITE_KEY = "fda_drug_safety"

CONFIG = {
    "name": "FDA Drug Safety Communications",
    "url_pattern": "fda.gov/drugs/drug-safety-and-availability/drug-safety-communications",
    "start_urls": ["https://www.fda.gov/drugs/drug-safety-and-availability/drug-safety-communications"],
    "max_pages": 10,
    # 列表页和详情页都是静态 HTML，用简单请求读，不开浏览器：浏览器开一个页面要 20-60 秒（等全部资源加载、被对方连接重置后
    # 重试），简单请求正常 1-2 秒；个别连接会卡住，由 scrapling_client 里的短超时加重试处理。
    "use_simple_fetch": True,
    # 列表是一张表（日期 | 标题），每行的标题是详情页链接。整张表（约 44 条）都在静态 HTML 里（浏览器里才被 DataTables
    # 分成每页 10 行），所以一页读完：没有“下一页”按钮，selector 留空。
    "list_item_selector": "table tbody td a",
    "pagination": {"type": "css_next_btn", "selector": ""},
    "detail_extractor": extract_fda_drug_safety,
}
