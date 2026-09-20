"""FDA Drug Safety Communications 详情页特解"""
from ._base import extract_base


def extract_fda_drug_safety(page) -> dict:
    return extract_base(page, body_selectors=[
        ".lcds-text-content", ".field--name-body", "main .content", "div.panel-group"
    ])
