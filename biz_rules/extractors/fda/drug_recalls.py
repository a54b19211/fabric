"""FDA Drug Recalls 详情页特解"""
from ._base import extract_base


def extract_fda_drug_recall(page) -> dict:
    return extract_base(page, body_selectors=[
        ".lcds-text-content", ".field--name-body", "main .content"
    ])
