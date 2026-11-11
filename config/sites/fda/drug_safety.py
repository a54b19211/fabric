from biz_rules.extractors.fda.drug_safety import extract_fda_drug_safety

SITE_KEY = "fda_drug_safety"

CONFIG = {
    "name": "FDA Drug Safety Communications",
    "url_pattern": "fda.gov/drugs/drug-safety-and-availability/drug-safety-communications",
    "start_urls": ["https://www.fda.gov/drugs/drug-safety-and-availability/drug-safety-communications"],
    "request_delay_seconds": 2.0,
    "max_pages": 10,
    "disable_resources": False,
    "solve_cloudflare": True,
    # 列表是一张表（日期 | 标题），每行的标题是详情页链接。
    "wait_selector": "table tbody td a",
    "list_item_selector": "table tbody td a",
    # 整张表（约 44 条）在浏览器里分页显示，地址上没有页码，只能点“下一页”（DataTables 的 li.next，翻到末页时变 disabled）
    "pagination": {
        "type": "js_click",
        "next_btn_selector": "li.paginate_button.next:not(.disabled) a",
    },
    "detail_extractor": extract_fda_drug_safety,
    "detail_wait_selector": "h1",
}
