"""openFDA Drug Enforcement（药品执法/召回）API 站点配置。"""
from biz_rules.extractors.fda._openfda_base import iter_openfda

SITE_KEY = "fda_api_enforcement"

CONFIG = {
    "name": "FDA Drug Enforcement (openFDA API)",
    "source_type": "api",
    "url_pattern": "api.fda.gov/drug/enforcement",
    "api": {
        "base_url": "https://api.fda.gov/drug/enforcement.json",
        "endpoint": "/drug/enforcement.json",
        "search": "",
        "max_records": 500,
    },
    "api_fetcher": iter_openfda,
    "uid_field": "recall_number",
    "field_map": {
        "title":        "product_description",
        "publish_date": "report_date",
        "summary":      "reason_for_recall",
        "drug_name":    "product_description",
        "manufacturer": "recalling_firm",
        "category":     "__const__:药品召回",
    },
}
