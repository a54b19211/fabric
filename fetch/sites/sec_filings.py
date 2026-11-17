"""SEC EDGAR 的 filing 原文：生物医药公司的定期报告 10-K / 10-Q / 20-F。8-K 见 fetch/sites/sec_8k.py（同一个读取函数）。

读法（都是 SEC 公开的官方地址）：company_tickers.json 把股票代码换成 CIK；data.sec.gov/submissions 给出一家公司最近的
filing 清单（新的在前）；Archives 里取 filing 的主文件（HTML），转成文字。8-K 的新闻稿在附件 EX-99 里，一并取。
一家公司一组（by_group）：续读位置记第几家，每家各自“读到已存的就停”，一个周期每家最多读最新 10 份（fetch/windows.py）。

SEC 的要求：请求头 User-Agent 要写明是谁（项目名 + 邮箱，DATAFABRIC_SEC_USER_AGENT 可改），每秒不超过 10 个请求
（来源目录里 sec.gov 的间隔是 1 秒）。单条原件上限 1 MiB：转成文字后还超过的（大公司的年报）记为读不到，不截断。
"""
import json
import os
import re
import warnings

from bs4 import BeautifulSoup, Comment, XMLParsedAsHTMLWarning

from fetch import transport
from fetch.api_fields import record
from store.record_models import MAX_RECORD_BYTES, GroupStart, ItemUnreadable

SITE_KEY = "sec_filings"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/"
_BLOCKS = ["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"]  # 单元格里有这些的行，不当一行写
_CONTAINERS = _BLOCKS + ["tr", "td", "th", "tbody", "thead", "tfoot", "ul", "ol", "body", "html", "section", "center",
                         "blockquote", "pre"]
# filing 自己的章节行（“PART II”“Item 1A. Risk Factors”）写成二级标题；目录表格里的同名行（带 | ）不算
_SECTION = re.compile(r"(?i)(?:PART\s+[IV]+(?:\s|$)|Item\s+\d+[A-Z]?\.\s+\S)")  # 只有“Item 1.”没有名字的是目录格子
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # inline XBRL 的 filing 带 XML 声明，照 HTML 读
_EXHIBIT_99 = re.compile(r"(?i)ex[a-z]*[-_]?99.*\.htm$")
# 美股上市的生物医药公司（股票代码）；外国公司报 20-F
TICKERS = ["PFE", "MRK", "LLY", "ABBV", "BMY", "AMGN", "GILD", "REGN", "VRTX", "BIIB", "MRNA", "JNJ", "AZN", "NVS",
           "GSK", "SNY", "NVO", "TAK", "BNTX", "ONC", "ZLAB", "LEGN", "HCM", "ALNY", "INCY", "BMRN", "NBIX", "UTHR",
           "EXEL", "JAZZ", "IONS", "SRPT", "INSM", "ARGX", "ROIV"]


def _get(url, cap=48 * 1024 * 1024):
    agent = os.environ.get("DATAFABRIC_SEC_USER_AGENT", "DataFabric-Research noreply@anthropic.com")
    response = transport.get(url, headers={"User-Agent": agent}, timeout=60, cap=cap)
    response.raise_for_status()
    return response


def html_text(html) -> str:
    """HTML → 一块一段的文字，一个字不丢：块（p / div / li / 标题 / 单元格…）各成段，块里直接写的文字也成段；
    单元格里没有别的块的表格行写成一段，单元格用 | 隔开。"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head", "ix:header"]):
        tag.decompose()
    lines = []
    _blocks(soup, lines)
    return "\n\n".join("## " + line if len(line) < 150 and " | " not in line and _SECTION.match(line) else line
                         for line in lines)


def _blocks(node, lines):
    """node 里的文字按文档顺序放进 lines：遇到块就先把攒着的行内文字收成一段，再进块里读。"""
    inline = []

    def flush():
        line = " ".join(" ".join(inline).split())
        inline.clear()
        if line:
            lines.append(line)

    for child in node.children:
        name = getattr(child, "name", None)
        if name is None:
            if not isinstance(child, Comment):
                inline.append(str(child))
        elif name == "tr" and not child.find(_BLOCKS):
            flush()
            cells = [" ".join(cell.get_text(" ").split()) for cell in child.find_all(["td", "th"])]
            inline.append(" | ".join(cell for cell in cells if cell))
            flush()
        elif name == "br":
            flush()
        elif name in _CONTAINERS or child.find(_CONTAINERS):
            flush()
            _blocks(child, lines)
        else:
            inline.append(child.get_text(" "))
    flush()


def _filing(api, company, cik, row):
    """一份 filing → 一条记录；主文件读不到或太大，抛 ItemUnreadable。"""
    base = f"{ARCHIVES}{cik}/{row['accessionNumber'].replace('-', '')}/"
    url = base + row["primaryDocument"]
    try:
        body = html_text(_get(url).content)
        if api.get("exhibits"):
            names = [item["name"] for item in _get(base + "index.json").json()["directory"]["item"]]
            for name in sorted(n for n in names if _EXHIBIT_99.search(n)):
                body += f"\n\n## Exhibit 99 ({name})\n\n" + html_text(_get(base + name).content)
    except Exception as exc:  # noqa: BLE001 -- 网络、状态码、格式：这一份读不到，接着读下一份
        raise ItemUnreadable(url, f"SEC filing 读取失败：{type(exc).__name__}") from None
    items = f"，Items {row['items']}" if row.get("items") else ""
    title = f"{company} {row['form']}（{row.get('reportDate') or row['filingDate']}{items}）"
    markdown = f"# {title}\n\n{body}"
    if len(json.dumps(markdown, ensure_ascii=False).encode()) > MAX_RECORD_BYTES - 4096:
        raise ItemUnreadable(url, f"filing 转成文字后 {len(markdown)} 字，超过单条 1 MiB 上限，没有存")
    return record(api["site"], row["accessionNumber"], title, row["filingDate"], markdown, url,
                  category="公司披露", summary=row.get("primaryDocDescription") or row["form"],
                  extra={k: row.get(k) for k in ("form", "accessionNumber", "filingDate", "reportDate", "items")}
                  | {"cik": str(cik), "company": company})


def iter_sec_filings(api, start=0):
    """从第 start 家公司读起：每家先给 GroupStart，再从新到旧给它 since 以后、forms 里的 filing。"""
    listed = {row["ticker"]: row for row in _get("https://www.sec.gov/files/company_tickers.json").json().values()}
    tickers = api.get("tickers", TICKERS)
    for number in range(start, len(tickers)):
        yield GroupStart(number)
        found = listed.get(tickers[number])
        url = f"https://data.sec.gov/submissions/CIK{int(found['cik_str']):010d}.json" if found else tickers[number]
        try:
            recent = _get(url).json()["filings"]["recent"]
        except Exception as exc:  # noqa: BLE001 -- 这家公司读不到：记下，接着读下一家
            yield ItemUnreadable(url, f"SEC 公司 filing 清单读取失败：{type(exc).__name__}")
            continue
        wanted = [i for i, form in enumerate(recent["form"])
                  if form in api["forms"] and recent["filingDate"][i] >= api.get("since", "2025-01-01")]
        for i in wanted[:api.get("per_company", 10)]:  # 一个周期每家最多读这么多份（和 fetch/windows.py 的 GROUP_LIMIT 一样）
            try:
                yield _filing(api, found["title"], found["cik_str"], {key: values[i] for key, values in recent.items()})
            except ItemUnreadable as unread:
                yield unread


CONFIG = {
    "name": "SEC EDGAR 定期报告（10-K / 10-Q / 20-F）",
    "source_type": "api",
    "url_pattern": "sec.gov",
    "by_group": True,
    "api": {"site": SITE_KEY, "forms": ["10-K", "10-Q", "20-F"], "since": "2025-01-01"},
    "api_fetcher": iter_sec_filings,
}
