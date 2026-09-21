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
        "max_records": 0,
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
    # 来源特有字段：与 biz_rules/schemas.py 的 FdaEnforcementExtra 字段对应
    # （openFDA 的召回级别字段名为 classification，需映射到 recall_class）
    "extra_fields": {
        "recall_number": "recall_number",
        "recall_initiation_date": "recall_initiation_date",
        "recall_class": "classification",
        "product_description": "product_description",
        "reason_for_recall": "reason_for_recall",
        "status": "status",
        "recalling_firm": "recalling_firm",
        "distribution_pattern": "distribution_pattern",
    },
}
