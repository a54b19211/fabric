"""FDA Press Announcements 详情页特解"""
from ._base import extract_base


def extract_fda_press(page) -> dict:
    # 正文先取 div[role=main]（原采集层 11-13 修正）：2026-11-14 实测新闻稿页面没有前两个类，原来落到 main article，
    # 正文开头多出“FDA News Release / 标题 / More Press Announcements”；div[role=main] 从“For Immediate Release”起
    return extract_base(page, body_selectors=[
        "div[role=main]", ".lcds-text-content", ".field--name-body", "article .content", "main article"
    ])
