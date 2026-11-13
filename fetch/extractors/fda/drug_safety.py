"""FDA Drug Safety Communications 详情页特解"""
import re

from fetch.extractors.dates import normalize_date

from ._base import extract_base

# 这份安全通讯发布的日期，页面上有两种写法：正文上方的小标题“03-20-2026 FDA Drug Safety Communication”（月-日-年），
# 和最新几篇标题下的副标题“FDA Drug Safety Communication – November 9, 2026”。两种都和列表里的日期一致。
# 页面上的 <time datetime> 是页脚“Content current as of”（最后一次改页面的日期，一批页面是同一天），不是发布日期。
_ISSUED = re.compile(r"(\d{1,2}-\d{1,2}-\d{4})\s+FDA Drug Safety Communication"
                     r"|FDA Drug Safety Communication\s*[–-]\s*([A-Z][a-z]+ \d{1,2}, \d{4})")


def extract_fda_drug_safety(page) -> dict:
    # div[role=main] 是整篇文章（侧栏、页脚之外）；旧选择器在一部分页面上匹配到的是空元素，只留作兜底
    result = extract_base(page, body_selectors=[
        'div[role="main"]', ".lcds-text-content", ".field--name-body", "main .content", "div.panel-group"
    ])
    issued = _ISSUED.search(page.get_all_text(strip=True))
    result["publish_date"] = normalize_date(issued[1] or issued[2]) if issued else ""
    return result
