"""arXiv 预印本（官方接口 export.arxiv.org/api/query）：q-bio 各类，标题 + 摘要，不抓全文。

按提交时间从新到旧，一页 100 条。arXiv 的使用条款要求两次请求至少隔 3 秒、不并发：间隔写在来源目录里
（fetch/catalog.py 的 arxiv 一行），所有请求都走 fetch.transport.get。一篇论文（去掉版本号的 arXiv 号）一条记录。
"""
import re
import xml.etree.ElementTree as ET

from fetch import transport
from fetch.api_fields import record

SITE_KEY = "arxiv"
API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
PAGE = 100


def _text(entry, tag):
    return " ".join((entry.findtext(ATOM + tag) or "").split())


def iter_arxiv(api):
    for start in range(0, api.get("max_results", 500), PAGE):
        response = transport.get(API, {"search_query": api["search_query"], "sortBy": "submittedDate",
                                       "sortOrder": "descending", "start": start, "max_results": PAGE}, timeout=60)
        response.raise_for_status()
        entries = ET.fromstring(response.content).findall(ATOM + "entry")
        for entry in entries:
            found = re.search(r"arxiv\.org/abs/(.+?)(v\d+)?$", _text(entry, "id"))
            if not found:
                continue
            title, abstract = _text(entry, "title"), _text(entry, "summary")
            yield record(
                SITE_KEY, found.group(1), title, _text(entry, "published")[:10], f"# {title}\n\n{abstract}",
                "https://arxiv.org/abs/" + found.group(1), category="预印本", summary=abstract,
                extra={"arxiv_id": found.group(1), "version": (found.group(2) or "").lstrip("v"),
                       "updated": _text(entry, "updated"),
                       "categories": [c.get("term") for c in entry.findall(ATOM + "category")],
                       "authors": [" ".join((a.findtext(ATOM + "name") or "").split())
                                   for a in entry.findall(ATOM + "author")]})
        if len(entries) < PAGE:
            return


CONFIG = {
    "name": "arXiv q-bio",
    "source_type": "api",
    "url_pattern": "arxiv.org",
    "api": {"search_query": "cat:q-bio.*", "max_results": 500},
    "api_fetcher": iter_arxiv,
}
