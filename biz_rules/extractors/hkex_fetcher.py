"""港交所披露易公告拉取。

接口：披露易“标题搜索”页上“更多”按钮背后的 JSON 接口 titleSearchServlet.do（GET）。参数 rowRange 是一次返回的最大条数
（页面每点一次“更多”加 100；服务端上限 10000）：传 10000，一次请求就拿到这家公司从 from_date 起的全部公告
（实测 239 条、1392 条都是一次请求），不用翻页、不用按日期分段。结果按发放时间从新到旧。
以前 POST 页面 titlesearch.xhtml，只能拿到最新 100 条，还把“文件类型”栏当成了标题。
"""

import html
import json
import re
from datetime import date
from typing import Dict, Iterator
from urllib.parse import urljoin

import requests
from loguru import logger

from biz_rules.history_models import HistoryError
from config.sites.hkex_pharma_list_auto import PHARMA_COMPANIES

SITE = "https://www1.hkexnews.hk"
ROW_RANGE = 10000  # 接口自己的上限


def _plain(value) -> str:
    """接口里的文字夹着 <br/> 这样的标签和 &#x2f;、&ndash; 这样的转义：去标签、还原转义、压缩空白。"""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def _day(release_time: str) -> str:
    """"05/11/2026 08:00"（日/月/年 时:分）→ "2026-11-05"。认不出来返回空串。"""
    found = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", release_time or "")
    return f"{found[3]}-{int(found[2]):02d}-{int(found[1]):02d}" if found else ""


def _parse_row(row: Dict, company: Dict, skip_headlines) -> Dict | None:
    """接口的一行 → 一条公告。不是公告的返回 None：文件类型栏在 skip_headlines 里（如“展示文件”，只是提示语）。
    title 是公告真正的标题（页面上文件链接的文字）；doc_type 是“文件类型”栏（如“公告及通告 - [...]”“月報表”）。"""
    doc_type = _plain(row.get("SHORT_TEXT"))
    if doc_type in skip_headlines:
        return None
    pdf_url = urljoin(SITE, row["FILE_LINK"]) if row.get("FILE_LINK") else ""
    title = _plain(row.get("TITLE"))
    return {
        "release_time": row.get("DATE_TIME", ""),
        "publish_date": _day(row.get("DATE_TIME")),
        "stock_code": company.get("code"),  # 从配置获取的股票代码
        "stock_code_extracted": row.get("STOCK_CODE", ""),
        "stock_name": row.get("STOCK_NAME", ""),
        "stock_id": company.get("stock_id"),
        "title": title,
        "doc_type": doc_type,
        "news_id": row.get("NEWS_ID", ""),
        "file_type": row.get("FILE_TYPE", ""),
        "file_size": row.get("FILE_INFO", ""),
        "pdf_url": pdf_url,
        "attachment_links": [{"url": pdf_url, "name": title}] if pdf_url else [],
    }


def _read_company(api_config: Dict, company: Dict) -> list[Dict]:
    """这家公司从 from_date 到 to_date 的全部公告（新的在前）。读不到就抛 HistoryError("source_read_failed")，写明哪家、为什么。"""
    who = f"{company.get('name')}({company.get('code')})"
    params = {
        "sortDir": 0,
        "sortByOptions": "DateTime",
        "category": 0,
        "market": api_config.get("market", "SEHK"),
        "stockId": company["stock_id"],
        "documentType": -1,
        "fromDate": api_config.get("from_date", "20240101"),
        "toDate": api_config.get("to_date") or date.today().strftime("%Y%m%d"),  # 不填 = 今天；接口不接受空的 to
        "title": "",
        "searchType": api_config.get("searchType", 0),
        "t1code": -2,
        "t2Gcode": -2,
        "t2code": -2,
        "rowRange": ROW_RANGE,
        "lang": api_config.get("lang", "ZH"),
    }
    try:
        # 超时（连接秒数，等响应秒数）：接口约 2-4 秒回；个别连接会卡住（实测披露易的两个 Akamai 节点里一个不通），
        # 连接 5 秒不通就放弃，由 collection.transport 换一条连接重试
        response = requests.get(api_config["base_url"], params=params, timeout=(5, 20),
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        response.raise_for_status()
        body = response.json()
        rows = json.loads(body["result"])
    except Exception as exc:  # noqa: BLE001 -- 网络、状态码、不是 JSON，都按“这家没读到”报，带上原因
        raise HistoryError("source_read_failed", f"港交所 {who} 读取失败：{type(exc).__name__} {exc}"[:300]) from exc
    if body.get("hasNextRow"):  # 超过接口上限 10000 条：不能当读完（from_date 往后调即可）
        raise HistoryError("source_read_failed", f"港交所 {who} 的公告超过 {ROW_RANGE} 条，请把 from_date 往后调")
    logger.info(f"[HKEX] {who} 共 {len(rows)} 条（接口报 {body.get('recordCnt')} 条）")
    return rows


class _Announcements:
    """所有公司的公告，一条一条给。读不到的公司在 next() 里抛 HistoryError；再调一次 next() 接着读下一家
    （生成器抛出后就结束了，做不到这样，所以用迭代器）。"""

    def __init__(self, api_config: Dict):
        self.api_config = api_config
        self.skip_headlines = api_config.get("skip_headlines", ())
        self.companies = iter(api_config.get("companies", PHARMA_COMPANIES))
        self.rows = iter(())
        self.company = {}
        self.seen = set()  # 联合公告会出现在几家公司名下，同一个文件只给一次

    def __iter__(self):
        return self

    def __next__(self) -> Dict:
        while True:
            for row in self.rows:
                announcement = _parse_row(row, self.company, self.skip_headlines)
                if announcement and announcement["pdf_url"] not in self.seen:
                    self.seen.add(announcement["pdf_url"])
                    return announcement
            self.company = next(self.companies)  # 没有下一家了：StopIteration，结束
            if not self.company.get("stock_id"):
                raise HistoryError("source_read_failed", f"港交所 {self.company.get('name')} 缺少 stock_id，没有读")
            self.rows = iter(_read_company(self.api_config, self.company))


def fetch_hkex_announcements(api_config: Dict) -> Iterator[Dict]:
    """从港交所披露易拉取上市公司公告"""
    logger.info(f"[HKEX] 公司数量: {len(api_config.get('companies', PHARMA_COMPANIES))}")
    return _Announcements(api_config)
