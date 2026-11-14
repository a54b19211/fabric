"""港交所披露易公告拉取。

接口：披露易“标题搜索”页上“更多”按钮背后的 JSON 接口 titleSearchServlet.do（GET）。参数 rowRange 是一次返回的最大条数
（页面每点一次“更多”加 100；服务端上限 10000）：传 10000，一次请求就拿到这家公司从 from_date 起的全部公告
（实测 239 条、1392 条都是一次请求），不用翻页、不用按日期分段。结果按发放时间从新到旧。
一家公司是一组（fetch.site_crawler 的分组读取）：每家公司开头给一个 GroupStart(第几家)，从第 start 家读起，
续读不用把前面的公司再请求一遍。
以前 POST 页面 titlesearch.xhtml，只能拿到最新 100 条，还把“文件类型”栏当成了标题。
"""

import html
import json
import re
from datetime import date
from typing import Dict, Iterator
from urllib.parse import urljoin

import httpx
from loguru import logger

from fetch import transport
from fetch.sites.hkex_pharma_list_auto import PHARMA_COMPANIES
from store.record_models import FabricError, GroupStart, ItemUnreadable

SITE = "https://www1.hkexnews.hk"
ROW_RANGE = 10000  # 接口自己的上限


def _plain(value) -> str:
    """接口里的文字夹着 <br/> 这样的标签和 &#x2f;、&ndash; 这样的转义：去标签、还原转义、压缩空白。"""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def _day(release_time: str) -> str:
    """"05/11/2026 08:00"（日/月/年 时:分）→ "2026-11-05"。认不出来返回空串。"""
    found = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", release_time or "")
    return f"{found[3]}-{int(found[2]):02d}-{int(found[1]):02d}" if found else ""


def _parse_row(row: Dict, company: Dict, skip_headlines, skip_titles) -> Dict | None:
    """接口的一行 → 一条公告。不是公告的返回 None：文件类型栏在 skip_headlines 里（如“展示文件”），或标题在 skip_titles 里
    （同一句“发行人刚在英文版发布了公告……”也会出现在普通文件类型下，链接到一页只有这句话的 .htm）——都只是提示语。
    title 是公告真正的标题（页面上文件链接的文字）；doc_type 是“文件类型”栏（如“公告及通告 - [...]”“月報表”）。"""
    doc_type, title = _plain(row.get("SHORT_TEXT")), _plain(row.get("TITLE"))
    if doc_type in skip_headlines or title in skip_titles:
        return None
    pdf_url = urljoin(SITE, row["FILE_LINK"]) if row.get("FILE_LINK") else ""
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
    """这家公司从 from_date 到 to_date 的全部公告（新的在前）。读不到就抛 FabricError("source_read_failed")，写明哪家、为什么。"""
    who = f"{company.get('name')}({company.get('code')})"
    if not company.get("stock_id"):
        raise FabricError("source_read_failed", f"港交所 {who} 缺少 stock_id，没有读")
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
        # 超时：接口约 2-4 秒回；个别连接会卡住（实测披露易的两个 Akamai 节点里一个不通），
        # 连接 5 秒不通就放弃，transport.get 换一条连接重试
        response = transport.get(api_config["base_url"], params, timeout=httpx.Timeout(20, connect=5), cap=32 * 1024 * 1024,
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        response.raise_for_status()
        body = response.json()
        rows = json.loads(body["result"])
    except Exception as exc:  # noqa: BLE001 -- 网络、状态码、不是 JSON，都按“这家没读到”报，带上原因
        raise FabricError("source_read_failed", f"港交所 {who} 读取失败：{type(exc).__name__} {exc}"[:300]) from exc
    if body.get("hasNextRow"):  # 超过接口上限 10000 条：不能当读完（from_date 往后调即可）
        raise FabricError("source_read_failed", f"港交所 {who} 的公告超过 {ROW_RANGE} 条，请把 from_date 往后调")
    logger.info(f"[HKEX] {who} 共 {len(rows)} 条（接口报 {body.get('recordCnt')} 条）")
    return rows


def _is_rmb_counter(company: Dict, codes: set) -> bool:
    """双柜台股票的人民币柜台（代码 8xxxx，港元柜台的代码加 80000），而且港元柜台也在清单里。两个柜台列的是同一批公告（实测 86618
    和 06618 的 139 份文件完全相同，每行的股票代码栏都是“06618<br/>86618”）：读两遍，同一份公告每次都在两个柜台的版本之间来回变
    （我们存的股票代码不同），每个周期都多出新版本。所以只读港元柜台。"""
    code = company.get("code") or ""
    return code.startswith("8") and code.isdigit() and str(int(code) - 80000).zfill(5) in codes


def fetch_hkex_announcements(api_config: Dict, start: int = 0) -> Iterator[Dict]:
    """从港交所披露易拉取上市公司公告，一条一条给，从第 start 家公司读起。每家公司开头先给 GroupStart（再发请求，请求超时
    停在这里，续读位置就是这一家）。读不到的公司给一个 ItemUnreadable（写明哪家、为什么，采集插件记下它、不存版本），接着读下一家。"""
    companies = api_config.get("companies", PHARMA_COMPANIES)
    skip_headlines, skip_titles = api_config.get("skip_headlines", ()), api_config.get("skip_titles", ())
    logger.info(f"[HKEX] 公司数量: {len(companies)}，从第 {start} 家读起")
    seen = set()  # 联合公告会出现在几家公司名下，同一个文件只给一次（续读后只管这一次读取里读到的）
    codes = {company.get("code") for company in companies}
    for number in range(start, len(companies)):
        company = companies[number]
        yield GroupStart(number)
        if _is_rmb_counter(company, codes):
            continue
        try:
            rows = _read_company(api_config, company)
        except FabricError as exc:
            yield ItemUnreadable(f"{api_config['base_url']}?stockId={company.get('stock_id')}", exc.message)
            continue
        for row in rows:
            announcement = _parse_row(row, company, skip_headlines, skip_titles)
            if announcement and announcement["pdf_url"] not in seen:
                seen.add(announcement["pdf_url"])
                yield announcement
