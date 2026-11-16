"""Reading a written number in a quote: the few rules of BioCheck's normalization (extraction_normalize.v1 and
v6, owned by BioCheck's app/normalize.py) that the measure producer needs, copied so the producer reads a number
the way BioCheck checks it. Keep in step with BioCheck when it changes these rules.

- Numbers: plain digits, thousands commas, or thousands grouped by a space, NO-BREAK SPACE or NARROW NO-BREAK SPACE -
  every group after the first exactly three digits, one separator kind per number; optional decimals. More than
  MAX_DIGITS significant digits or MAX_DECIMALS decimals is not a number here.
- v6: a count written as one of the words one to twelve is a number, standing as a whole word.
"""

import re
from decimal import Decimal, InvalidOperation

MAX_DIGITS, MAX_DECIMALS = 18, 6
GROUP_SPACES = "   "
FULLWIDTH = str.maketrans("０１２３４５６７８９．，－", "0123456789.,-")
_SPACE_GROUPED = r"\d{1,3}(?:(?P<sep>[   ])\d{3})(?:(?P=sep)\d{3})*"
# Scanned left to right: a grouped number is consumed whole, so its tail ("851" of "7 851") never starts a token;
# a four-digit run cannot head a space group, so "2023 1 444" is two numbers.
_TOKEN = re.compile(
    r"(?<![\d.,])[-−]?(?:\d{1,3}(?:,\d{3})+|" + _SPACE_GROUPED + r"|\d+)(?:\.\d+)?(?![\d]|[.,]\d)"
)
_WHOLE = re.compile(r"[-−]?(?:\d{1,3}(?:,\d{3})+|" + _SPACE_GROUPED + r"|\d+)(?:\.\d+)?")

def decimal_text(written):
    """The canonical decimal string of a written number, or None (never through a float)."""
    t = (written or "").strip().translate(FULLWIDTH).replace("−", "-")
    if not _WHOLE.fullmatch(t):
        return None
    plain = t.replace(",", "")
    for s in GROUP_SPACES:
        plain = plain.replace(s, "")
    try:
        d = Decimal(plain)
    except InvalidOperation:
        return None
    text = format(d.normalize(), "f") if d != 0 else "0"
    whole, _, frac = text.lstrip("-").partition(".")
    if len(whole.lstrip("0") + frac) > MAX_DIGITS or len(frac) > MAX_DECIMALS:
        return None
    return text


def number_tokens(text):
    """[(start, end)] of every written number in text, a grouped number being one token."""
    t = (text or "").translate(FULLWIDTH)
    return [m.span() for m in _TOKEN.finditer(t)]


def is_whole_number(text, start, end):
    """Whether text[start:end] is exactly one whole number token (not part of a grouped number)."""
    return (start, end) in number_tokens(text)


SPELLED_NUMBERS = {word: n for n, word in enumerate(
    ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"), 1)}
_WORD_EDGE = "-–—'’"  # a hyphen or an apostrophe keeps the word part of a longer one: "one-third", "one's"


def decimal_text_v6(written):
    """decimal_text (v1), and a spelled number of one to twelve (any case) as its decimal text."""
    spelled = SPELLED_NUMBERS.get((written or "").strip().casefold())
    return str(spelled) if spelled else decimal_text(written)


def is_whole_number_v6(text, start, end):
    """is_whole_number (v1), or text[start:end] is a spelled number of one to twelve standing as a whole word."""
    if is_whole_number(text, start, end):
        return True
    if text[start:end].casefold() not in SPELLED_NUMBERS:
        return False
    around = (text[start - 1] if start > 0 else " ", text[end] if end < len(text) else " ")
    return not any(c.isalnum() or c in _WORD_EDGE for c in around)
