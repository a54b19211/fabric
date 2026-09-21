"""
药品站点（NMPA/CDE）详情页字段提取的公共工具。

这些函数从已清洗的正文文本中，用正则精确提取：
  - 批准文号（国药准字/注册证号/批准文号）
  - 带标签的字段值（药品名称/生产企业等）
"""

import re

from biz_rules.cleaners import full_to_half

# 批准文号：国药准字 + 一个类别字母（H/Z/S/B/J/F）+ 8 位数字
_APPROVAL_NUMBER_RE = re.compile(r"国药准字[ZHBSJF]\d{8}")

# 带标签的批准文号 / 注册证号
_APPROVAL_LABELED_RE = re.compile(r"(?:批准文号|注册证号)[:：]?\s*([ZHBSJF]?\d{8})")

# 标签字段取值：标签名后接冒号/空白，再取到下一个空白或中文标点为止
_LABEL_VALUE_RE = re.compile(r"[:：]?\s*([^\s，。；、,，:：]{1,40})")


def extract_approval_number(text: str) -> str:
    """从正文中精确提取批准文号，取不到返回空字符串。"""
    if not text:
        return ""
    text = full_to_half(text)
    m = _APPROVAL_NUMBER_RE.search(text)
    if m:
        return m.group(0)
    m = _APPROVAL_LABELED_RE.search(text)
    if m:
        return m.group(1)
    return ""


def extract_labeled_field(text: str, labels: tuple[str, ...]) -> str:
    """提取某个标签（如"药品名称""生产企业"）后紧跟的字段值。"""
    if not text:
        return ""
    text = full_to_half(text)
    for label in labels:
        m = re.search(re.escape(label) + _LABEL_VALUE_RE.pattern, text)
        if m:
            value = m.group(1).strip()
            if value:
                return value
    return ""
