"""bioRxiv / medRxiv 预印本（官方接口 api.biorxiv.org，两个站一个读法）：标题 + 摘要，不抓全文。

接口按日期区间给出当天新发和改版的预印本，一页 30 条、旧的在前。定时刷新要从新到旧，所以从今天往回一天一天读，
每天读完整天再倒过来给；连续读到已存的就停（fetch/windows.py），最多回看 days 天。一篇预印本（DOI）一条记录，
改版（v2、v3）是这条记录的新版本。medRxiv 见 fetch/sites/medrxiv.py（同一个函数，server 不同）。
"""
from datetime import datetime, timedelta, timezone

from fetch import transport
from fetch.api_fields import record

SITE_KEY = "biorxiv"
API = "https://api.biorxiv.org/details/{server}/{day}/{day}/{cursor}/json"


def _day(server, day):
    """这一天的全部条目（接口的顺序）。"""
    papers, total = [], None
    while total is None or len(papers) < total:
        response = transport.get(API.format(server=server, day=day, cursor=len(papers)), timeout=30)
        response.raise_for_status()
        data = response.json()
        batch = data.get("collection") or []
        total = int(((data.get("messages") or [{}])[0]).get("total") or 0)
        if not batch:
            break
        papers += batch
    return papers


def iter_openrxiv(api):
    """从今天往回，一天一天给出预印本（新的在前）；同一篇在这次读取里只给最新的一版。"""
    server, seen = api["server"], set()
    today = datetime.now(timezone.utc).date()
    for back in range(api.get("days", 14)):
        for paper in reversed(_day(server, (today - timedelta(days=back)).isoformat())):
            doi = paper.get("doi")
            if not doi or doi in seen:
                continue
            seen.add(doi)
            title, abstract = " ".join((paper.get("title") or "").split()), (paper.get("abstract") or "").strip()
            yield record(
                server, doi, title, paper.get("date"), f"# {title}\n\n{abstract}",
                f"https://www.{server}.org/content/{doi}v{paper.get('version') or 1}",
                category="预印本", summary=abstract,
                extra={k: paper.get(k) for k in ("doi", "version", "category", "authors", "license", "type",
                                                 "author_corresponding_institution", "published")})


CONFIG = {
    "name": "bioRxiv",
    "source_type": "api",
    "url_pattern": "biorxiv.org",
    "api": {"server": "biorxiv", "days": 14},
    "api_fetcher": iter_openrxiv,
}
