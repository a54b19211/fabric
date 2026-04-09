from biz_rules.extractors.fda.drug_recalls import extract_fda_drug_recall

SITE_KEY = "fda_drug_recalls"

CONFIG = {
    "name": "FDA Drug Recalls",
    "url_pattern": "fda.gov/drugs/drug-safety-and-availability/drug-recalls",
    "start_urls": ["https://www.fda.gov/drugs/drug-safety-and-availability/drug-recalls"],
    "request_delay_seconds": 2.0,
    "max_pages": 10,
    "disable_resources": False,
    "solve_cloudflare": True,
    "wait_selector": "table",
    "list_item_selector": "table tbody tr td a",
    "pagination": {
        "type": "url_pattern",
        "subtype": "query_param",
        "param": "page",
        "start": 0,
    },
    "detail_extractor": extract_fda_drug_recall,
    "detail_wait_selector": "h1",
}
