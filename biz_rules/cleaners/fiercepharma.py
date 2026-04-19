"""
Fierce Pharma 内容清洗器。

问题：markdown 开头有大量导航栏/菜单文字堆叠，污染图谱提取。
策略：找到正文起始点（作者行或第一个实质段落），截断前面的噪音。
"""

import re


# 导航栏中反复出现的噪音词，用于识别噪音区域
_NAV_NOISE_PATTERNS = [
    r"^Fierce\s+(Pharma|Biotech|Healthcare|Life Sciences)",
    r"^(Advertise|About Us|Subscribe|Resources|Events|Podcasts|Webinars|Whitepapers|Survey)$",
    r"^(Pharma|Vaccines|Asia|Manufacturing|Facilities|Recalls|Warning Letters|Marketing)$",
    r"^(Cell & Gene Therapy|Drug Delivery|Executives|Special Reports|Fierce 50)$",
    r"^(Oncology|Supply Chain|AI and Machine Learning|Awards|Outsourcing Awards)$",
    r"^(Innovation Awards|Pharma Marketing Awards|Industry Events)$",
]
_NAV_RE = re.compile("|".join(_NAV_NOISE_PATTERNS), re.MULTILINE)

# 正文起始标志：作者行（By xxx）或日期行
_BODY_START_RE = re.compile(
    r"^(By\s+\w|"                          # By 作者名
    r"\w{3}\s+\d{1,2},\s+\d{4}|"          # Apr 3, 2026
    r"\d{4}-\d{2}-\d{2})",                 # 2026-04-03
    re.MULTILINE,
)


def clean_fiercepharma(parsed_result: dict) -> dict:
    """去除 Fierce Pharma markdown 开头的导航栏噪音。"""
    md = parsed_result.get("markdown", "")
    if not md:
        return parsed_result

    lines = md.split("\n")
    title_line = lines[0] if lines else ""  # 保留 # 标题行

    # 找正文起始行索引（跳过标题行本身）
    body_start = None
    for i, line in enumerate(lines[1:], start=1):
        stripped = line.strip()
        if not stripped:
            continue
        # 如果这行不是导航噪音，且不是纯链接/单词，认为是正文开始
        if not _NAV_RE.match(stripped) and len(stripped) > 20:
            body_start = i
            break

    if body_start is None:
        # 兜底：找 "By " 作者行
        for i, line in enumerate(lines):
            if _BODY_START_RE.match(line.strip()):
                body_start = i
                break

    if body_start and body_start > 1:
        cleaned_lines = [title_line, ""] + lines[body_start:]
        parsed_result["markdown"] = "\n".join(cleaned_lines).strip()

    # 去除结尾的版权/导航尾巴
    md = parsed_result["markdown"]
    footer_markers = [
        "Let Google know we are your trusted source.",
        "©", "All rights reserved", "Terms of use", "Privacy Policy",
        "Connect\nThe Team", "Let's Talk\nJoin Us",
    ]
    for marker in footer_markers:
        idx = md.find(marker) if marker == "Let Google know we are your trusted source." else md.rfind(marker)
        if idx != -1 and idx > len(md) // 4:  # 排除正文开头误截
            md = md[:idx].strip()
            break  # 找到最早的截断点就停
    parsed_result["markdown"] = md

    return parsed_result
