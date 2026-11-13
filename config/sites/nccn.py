"""NCCN (National Comprehensive Cancer Network) 临床实践指南站点配置。

数据来源：https://www.nccn.org/guidelines/category_1 (及 category_2/3/4)
采集策略：
  1. httpx 抓取 4 个分类列表页，解析所有指南条目
  2. StealthyFetcher 逐一渲染详情页，提取 PDF 链接、版本号等
"""
from biz_rules.extractors.nccn import iter_nccn

SITE_KEY = "nccn"

CONFIG = {
    "name": "NCCN Clinical Practice Guidelines",
    "source_type": "api",
    "url_pattern": "nccn.org",
    "api": {
        # 要抓取的分类
        "categories": [
            "Treatment by Cancer Type",
            "Detection, Prevention, and Risk Reduction",
            "Supportive Care",
            "Specific Populations",
        ],
        # 0 = 全量
        "max_per_category": 0,
    },
    "api_fetcher": iter_nccn,
    "by_group": True,  # 一份指南一组：续读位置记组号（分类编号 × 1000 + 序号，见 nccn_fetcher；collection.site_crawler）
    # iter_nccn 内部已生成 UUID，field_map 直接映射
    "uid_field": "",
    "field_map": {
        "title":        "title",
        "publish_date": "version",          # 用版本年份作为发布日期
        "summary":      "markdown",
        "category":     "__const__:临床指南",
        "source_url":   "source_url",
        "sniffed_meta.version":       "version",
        "sniffed_meta.pdf_url":       "pdf_url",
        "sniffed_meta.category_name": "category_name",
        "sniffed_meta.guideline_id":  "guideline_id",
    },
}
