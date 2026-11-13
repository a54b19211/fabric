"""
港交所披露易站点配置
"""

from fetch.extractors.hkex_fetcher import fetch_hkex_announcements
from fetch.sites.hkex_pharma_list_auto import PHARMA_COMPANIES

SITE_KEY = "hkex"

CONFIG = {
    "name": "港交所披露易",
    "source_type": "api",
    "api_fetcher": fetch_hkex_announcements,
    "by_group": True,  # 一家公司一组：续读位置记第几家公司，“遇到已存的就停”按公司算（fetch.site_crawler）

    "api": {
        # 标题搜索页“更多”按钮背后的 JSON 接口；一次请求读完一家公司从 from_date 起的全部公告，见 hkex_fetcher
        "base_url": "https://www1.hkexnews.hk/search/titleSearchServlet.do",
        "lang": "ZH",
        "market": "SEHK",
        "searchType": 0,
        "from_date": "20240101",
        # 截止日，格式 YYYYMMDD；留空 = 读取当天。接口要求 to 必须有值：不传或传空串不报错，但一条都查不到（实测）；
        # 写成将来的日期没问题。以前写死成 20260410，之后发布的公告全读不到。
        "to_date": "",
        # 不是公告的条目：文件类型栏为“展示文件”的行，内容只是一句“发行人刚在英文版发布了公告，中文版本或会/或不会在本版面发布”，
        # 各公司指向同一个网址、没有任何信息，直接跳过。
        "skip_headlines": ["展示文件"],
        # 同一句提示语也会出现在普通文件类型（如“公告及通告 - [海外监管公告-其他]”）下，链接到一页只有这句话的 .htm：按标题跳过。
        "skip_titles": ["發行人剛在本網站英文版面發布一項公告，相應中文版本或會／或不會在本版面發布。"],
        "companies": PHARMA_COMPANIES,
    },

    "uid_field": "pdf_url",
    "field_map": {
        "title": "title",  # 公告真正的标题；文件类型栏（如“公告及通告 - [...]”“月報表”）在 doc_type，放进摘要
        "publish_date": "publish_date",
        "summary": "doc_type",
        "source_url": "pdf_url",
        "stock_code": "stock_code",
        "stock_name": "stock_name",
    }
}
