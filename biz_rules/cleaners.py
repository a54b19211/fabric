"""
==========================================================
  文本清洗与初筛工具函数
  ★ 药学研究生专属文件 ★ —— 正则清洗规则都放这里
==========================================================

【使用说明】
- 每个函数做一件小事：清洗文本、去掉垃圾字符、格式化日期等。
- 想加新的清洗规则？模仿已有函数写一个新函数就行。
- 所有函数都是"输入字符串 → 输出字符串"，没有任何复杂逻辑。
"""

import re


def remove_extra_whitespace(text: str) -> str:
    """
    去除文本中多余的空白字符（连续空格变一个，连续换行变两个）。

    示例:
        输入: "你好    世界\\n\\n\\n\\n新段落"
        输出: "你好 世界\\n\\n新段落"
    """
    # 连续空格（不含换行）合并为一个
    text = re.sub(r"[^\S\n]+", " ", text)
    # 连续三个以上换行合并为两个
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def remove_html_tags(text: str) -> str:
    """
    去除残留的 HTML 标签。

    示例:
        输入: "<p>这是<b>粗体</b>文本</p>"
        输出: "这是粗体文本"
    """
    return re.sub(r"<[^>]+>", "", text)


def normalize_date(date_str: str) -> str:
    """
    尝试把常见的中文日期格式统一为 YYYY-MM-DD。

    支持格式:
        - 2024年03月15日 → 2024-03-15
        - 2024/03/15     → 2024-03-15
        - 2024.03.15     → 2024-03-15

    如果无法识别，原样返回。
    """
    # 匹配 "2024年03月15日" 格式
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", date_str)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"

    # 匹配 "2024/03/15" 或 "2024.03.15" 格式
    match = re.search(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", date_str)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"

    return date_str.strip()


def clean_title(title: str) -> str:
    """
    清理标题：去首尾空格、去 HTML 标签、去多余空白。

    示例:
        输入: "  <span>关于加强药品管理的通知</span>  "
        输出: "关于加强药品管理的通知"
    """
    title = remove_html_tags(title)
    title = remove_extra_whitespace(title)
    return title


def truncate_text(text: str, max_length: int = 5000) -> str:
    """
    截断超长文本，防止喂给大模型时超出 token 限制。

    参数:
        text: 原始文本
        max_length: 最大字符数，默认 5000

    返回:
        截断后的文本（如有截断会加上提示）
    """
    if len(text) <= max_length:
        return text
    return text[:max_length] + "\n\n...[文本已截断]..."
