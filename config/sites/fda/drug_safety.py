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
    "wait_selector": "h3.lcds-text-heading",
    "list_item_selector": "h3.lcds-text-heading a",
    "pagination": {
        "type": "url_pattern",
        "subtype": "query_param",
        "param": "page",
        "start": 0,
    },
    "detail_extractor": extract_fda_drug_safety,
    "detail_wait_selector": "h1",
}
