"""
药品站点（NMPA/CDE）详情页字段提取的公共工具。

这些函数从已清洗的正文文本中，用正则精确提取：
  - 批准文号（国药准字/注册证号/批准文号）
  - 带标签的字段值（药品名称/生产企业等）
"""

import re

from biz_rules.cleaners import full_to_half
from processing import identifiers

# 标签字段取值：标签名后接冒号/空白，再取到下一个空白或中文标点为止
_LABEL_VALUE_RE = re.compile(r"[:：]?\s*([^\s，。；、,，:：]{1,40})")


def extract_approval_number(text: str) -> str:
    """从正文中精确提取批准文号，取不到返回空字符串。"""
    if not text:
        return ""
    hits = identifiers.extract(text)
    for kind in ("nmpa_approval", "nmpa_registration"):  # 编号规则只在 processing.identifiers
        for hit in hits:
            if hit["kind"] == kind:
                return hit["value"]
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
