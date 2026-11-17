"""港交所披露易·定期报告（年报、中期报告、季度报告、ESG 报告）：和 fetch/sites/hkex.py 同一个接口和读取函数，
只是按标题类别“财务报表/环境、社会及管治资料”（t1code 40000）筛。报告本身是 PDF 附件，正文由附件解析产出。"""
from fetch.sites.hkex import CONFIG as HKEX

SITE_KEY = "hkex_reports"

CONFIG = {**HKEX, "name": "港交所披露易·定期报告",
          "api": {**HKEX["api"], "searchType": 1, "t1code": 40000, "from_date": "20250101"}}
