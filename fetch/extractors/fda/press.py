"""FDA Press Announcements 详情页特解"""
from ._base import extract_base


def extract_fda_press(page) -> dict:
    return extract_base(page, body_selectors=[
        ".lcds-text-content", ".field--name-body", "article .content", "main article"
    ])
