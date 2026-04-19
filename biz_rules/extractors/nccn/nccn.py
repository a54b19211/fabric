"""NCCN 指南详情页解析器（CSS 特解）。

详情页结构（渲染后）：
  h3                    → 指南标题
  h4                    → section 名称（Guidelines / Evidence Blocks / Frameworks /
                          Guidelines for Patients / International / ...）
  section 内 a[href$=.pdf] + 后续文本 "Version X.YYYY"  → PDF 条目

输出：
  {
    title:    str,
    version:  str,          # 主 Guidelines PDF 的版本号
    sections: [             # 按 h4 分组的所有 PDF
      {
        section_name: str,
        pdfs: [
          { label: str, url: str, version: str }
        ]
      }
    ],
    attachment_links: [     # 扁平化列表，供 storage_writer 使用
      { name: str, url: str, version: str, section: str }
    ],
    body_text: str,         # 页面正文纯文本（用于 markdown 落盘）
  }
"""
import re


def _abs(href: str) -> str:
    """补全相对路径为绝对 URL。"""
    if href.startswith("http"):
        return href
    return "https://www.nccn.org" + href


def extract_nccn_detail(page) -> dict:
    result = {
        "title": "",
        "version": "",
        "sections": [],
        "attachment_links": [],
        "body_text": "",
    }

    # ── 标题：h3 ──────────────────────────────────────────────────────────────
    h3_els = page.css("h3")
    if h3_els:
        result["title"] = h3_els[0].get_all_text(strip=True)

    # ── 按 h4 分组，收集每个 section 的 PDF ──────────────────────────────────
    # 策略：遍历页面所有元素，遇到 h4 切换当前 section，遇到 a[href$=.pdf] 归入当前 section
    current_section = "General"
    sections: dict[str, list] = {}   # section_name → [pdf_dict, ...]

    # scrapling 的 page 对象支持 css()，但不支持按文档顺序混合遍历 h4 和 a
    # 改用：先收集所有 h4 的文本和位置，再对每个 a[href$=.pdf] 判断它属于哪个 section
    # 实现方式：用 page 的 html_content 转 BeautifulSoup 做精确解析
    try:
        from bs4 import BeautifulSoup
        # 取主内容区域的 HTML
        main_els = page.css("main") or page.css("div.content-wrapper") or page.css("body")
        html_src = main_els[0].html_content if main_els else ""
        soup = BeautifulSoup(html_src, "html.parser")

        current_section = "General"
        for el in soup.find_all(["h4", "a"]):
            if el.name == "h4":
                current_section = el.get_text(strip=True)
            elif el.name == "a":
                href = el.get("href", "")
                if not href.endswith(".pdf"):
                    continue
                href = _abs(href)
                label = el.get_text(strip=True)

                # 版本号：在 <a> 后面的文本节点里
                version = ""
                parent_text = el.parent.get_text(" ", strip=True) if el.parent else ""
                after = parent_text[parent_text.find(label) + len(label):]
                ver_m = re.search(r"Version\s+([\d.]+)", after[:80])
                if ver_m:
                    version = ver_m.group(1)

                if current_section not in sections:
                    sections[current_section] = []
                sections[current_section].append({
                    "label": label,
                    "url": href,
                    "version": version,
                })

    except Exception:
        # BeautifulSoup 不可用时降级：直接用 css() 收集所有 PDF 链接
        for a_el in page.css("a[href]"):
            href = a_el.attrib.get("href", "")
            if not href.endswith(".pdf"):
                continue
            href = _abs(href)
            label = a_el.get_all_text(strip=True)
            parent_text = a_el.parent.get_all_text(strip=True) if a_el.parent else ""
            ver_m = re.search(r"Version\s+([\d.]+)", parent_text)
            version = ver_m.group(1) if ver_m else ""
            if "General" not in sections:
                sections["General"] = []
            sections["General"].append({"label": label, "url": href, "version": version})

    # ── 组装 sections 列表 ────────────────────────────────────────────────────
    for sec_name, pdfs in sections.items():
        result["sections"].append({"section_name": sec_name, "pdfs": pdfs})

    # ── 扁平化 attachment_links（storage_writer 格式）────────────────────────
    for sec in result["sections"]:
        for pdf in sec["pdfs"]:
            result["attachment_links"].append({
                "name": pdf["label"],
                "url": pdf["url"],
                "version": pdf["version"],
                "section": sec["section_name"],
            })

    # ── 主版本号：取 Guidelines section 第一个 PDF 的版本 ────────────────────
    for sec in result["sections"]:
        if "guideline" in sec["section_name"].lower():
            if sec["pdfs"]:
                result["version"] = sec["pdfs"][0]["version"]
                break
    # 兜底：取第一个有版本号的 PDF
    if not result["version"]:
        for att in result["attachment_links"]:
            if att["version"]:
                result["version"] = att["version"]
                break

    # ── 正文文本 ──────────────────────────────────────────────────────────────
    main_els = page.css("main") or page.css("div.content-wrapper") or page.css("body")
    if main_els:
        result["body_text"] = main_els[0].get_all_text(strip=True)
    else:
        result["body_text"] = page.get_all_text(strip=True)

    return result
