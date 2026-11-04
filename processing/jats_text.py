"""加工层：PMC 全文 XML（JATS）→ 带定位的正文。由 processing.attachment_parse 对 .xml 附件调用。

只读原件里的结构，不调用外部服务：标题、摘要、正文各节的标题/段落/表格/图注/列表，按文档顺序拼成正文
（块之间空一行，节标题加 Markdown #）。参考文献（back/ref-list）不进正文。
定位 locator_kind=jats_section：每块记在正文里的起止偏移、所在节路径（节号+节标题）和 JATS 路径
（如 body/sec[2]/p[3]）；JATS 没有页码，不编页码。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

LOCATOR_KIND = "jats_section"
CONTAINERS = {"disp-quote", "boxed-text", "statement", "sec-meta"}


def _flat(node) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _table(node) -> str:
    caption = " ".join(x for x in (_flat(node.find("label")), _flat(node.find("caption"))) if x)
    rows = []
    for tr in node.iter("tr"):
        cells = [_flat(cell) for cell in tr if cell.tag in ("td", "th")]
        if any(cells):
            rows.append(" | ".join(cells))
    foot = _flat(node.find("table-wrap-foot"))
    return "\n".join(x for x in (caption, *rows, foot) if x)


class _Builder:
    def __init__(self):
        self.parts, self.segments, self.offset = [], [], 0

    def add(self, kind, body, section, path):
        body = body.strip()
        if not body:
            return
        if self.parts:
            self.offset += 2  # "\n\n"
        self.segments.append({"start": self.offset, "end": self.offset + len(body), "type": kind,
                              "section": " > ".join(section) or None, "jats_path": path})
        self.parts.append(body)
        self.offset += len(body)

    def walk(self, node, path, section):
        counts = {}
        for child in node:
            if not isinstance(child.tag, str):
                continue
            counts[child.tag] = counts.get(child.tag, 0) + 1
            here = path + "/" + child.tag + "[" + str(counts[child.tag]) + "]"
            tag = child.tag
            if tag == "sec":
                heading = " ".join(x for x in (_flat(child.find("label")), _flat(child.find("title"))) if x)
                inner = section + [heading] if heading else section
                if heading:
                    self.add("heading", "#" * min(6, len(inner) + 1) + " " + heading, inner, here + "/title")
                self.walk(child, here, inner)
            elif tag == "p":
                self.add("paragraph", _flat(child), section, here)
            elif tag == "table-wrap":
                self.add("table", _table(child), section, here)
            elif tag == "fig":
                label = " ".join(x for x in (_flat(child.find("label")), _flat(child.find("caption"))) if x)
                self.add("figure_caption", label, section, here)
            elif tag in ("list", "def-list"):
                items = [_flat(item) for item in child
                         if isinstance(item.tag, str) and item.tag in ("list-item", "def-item")]
                self.add("list", "\n".join("- " + x for x in items if x), section, here)
            elif tag in CONTAINERS:
                self.walk(child, here, section)


def parse(path) -> dict | None:
    """返回 {"text", "segments", "locator_kind"}；不是 JATS 文章返回 None。XML 损坏抛 ValueError。"""
    raw = Path(path).read_bytes()
    if b"<!ENTITY" in raw[:4096]:
        raise ValueError("entity_declaration")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError("invalid_xml") from None
    article = root if root.tag == "article" else root.find("article")
    if article is None:
        return None
    builder = _Builder()
    meta = "front/article-meta"
    title = article.find(meta + "/title-group/article-title")
    builder.add("title", "# " + _flat(title) if _flat(title) else "", [], meta + "/title-group/article-title")
    for index, abstract in enumerate(article.findall(meta + "/abstract"), start=1):
        name = _flat(abstract.find("title")) or "Abstract"
        here = meta + "/abstract[" + str(index) + "]"
        builder.add("heading", "## " + name, [name], here)
        builder.walk(abstract, here, [name])
    body = article.find("body")
    if body is not None:
        builder.walk(body, "body", [])
    return {"text": "\n\n".join(builder.parts), "segments": builder.segments, "locator_kind": LOCATOR_KIND}
