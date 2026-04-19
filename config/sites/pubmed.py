"""PubMed E-utilities API 站点配置。"""
from biz_rules.extractors.pubmed import iter_pubmed

SITE_KEY = "pubmed"

CONFIG = {
    "name": "PubMed 医学文献",
    "source_type": "api",
    "url_pattern": "pubmed.ncbi.nlm.nih.gov",
    "api": {
        # PubMed 检索式，支持完整的 PubMed 语法
        # 示例：药品安全相关文献，最近 1 年
        "query": 'drug safety[Title/Abstract] AND ("2024"[Date - Publication] : "2025"[Date - Publication])',
        "max_records": 200,   # 调试用，正式跑可改大或设 0 拉全量
        "db": "pubmed",
        # "api_key": "",      # 填入 NCBI API Key 可提升速率至 10 req/s
        "fetch_fulltext": True,          # 是否尝试获取全文（PMC + Unpaywall）
        "unpaywall_email": "xli85042@gmail.com",           # 填入邮箱，Unpaywall 要求提供联系方式
    },
    "api_fetcher": iter_pubmed,
    "uid_field": "pmid",
    "field_map": {
        "title":                "title",
        "publish_date":         "publish_date",
        "summary":              "abstract",
        "drug_name":            "",
        "manufacturer":         "",
        "category":             "__const__:医学文献",
        "source_url":           "source_url",           # 每篇文章独立 URL
        "sniffed_meta.doi":         "doi",              # 透传给置信度评估
        "sniffed_meta.journal_name": "journal_name",
    },
}
