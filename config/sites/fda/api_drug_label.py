"""openFDA Drug Label（药品说明书）API 站点配置。"""
from biz_rules.extractors.fda._openfda_base import iter_openfda

SITE_KEY = "fda_api_drug_label"

CONFIG = {
    "name": "FDA Drug Label (openFDA API)",
    "source_type": "api",
    "url_pattern": "api.fda.gov/drug/label",
    "api": {
        "base_url": "https://api.fda.gov/drug/label.json",
        "endpoint": "/drug/label.json",
        "search": "",
        "sort": "effective_time:asc",
        "max_records": 0,
    },
    "api_fetcher": iter_openfda,
    "uid_field": "set_id",
    "field_map": {
        "title":        "openfda.brand_name.0",
        "publish_date": "effective_time",
        "summary":      "purpose.0",
        "drug_name":    "openfda.generic_name.0",
        "manufacturer": "openfda.manufacturer_name.0",
        "category":     "__const__:药品说明书",
    },
}
