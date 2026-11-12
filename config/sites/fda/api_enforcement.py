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
        # 新的在前。不指定时顺序是乱的（实测前三条是 2015、2020、2025 年的），固定刷新“遇到已存的就停”要从新到旧。
        "sort": "report_date:desc",
        "max_records": 0,
    },
    "api_fetcher": iter_openfda,
    "uid_field": "recall_number",
    "field_map": {
        "title":        "product_description",
        "publish_date": "report_date",
        "summary":      "reason_for_recall",
        # 药名候选链：openfda 结构化名优先（规范通用名/品牌名），整段产品描述兜底。
        # 此前单映射 product_description，导致 drug_name 是 200 字的包装+企业+NDC
        # 描述，既不是药名、也无法直接送权威库（只能靠归一器截断）。
        # 注意：兜底时保留原值不动，截断由 biz_rules.drug_name_normalizer 负责。
        "drug_name":    [
            "openfda.generic_name.0",
            "openfda.brand_name.0",
            "openfda.substance_name.0",
            "product_description",
        ],
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
        # FDA 上市申请号（NDA/ANDA/BLA + 6 位），来自 openfda 子对象。
        # 补这一条是因为 fda 此前完全没有可校验的标识符字段，T3 标识符对齐
        # 在这两个站点上是 0 输入。有 openfda 的记录可直接过本地格式校验。
        "approval_number": "openfda.application_number.0",
    },
}
