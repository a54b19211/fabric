"""The window receipts a listing of a windowed source carries (task book 01, lead channel).

A windowed read (biz_rules.gdelt_news) leaves a receipt per window in its run's coverage. A list of the records of a
window that left them out would read as the whole window: the receipts say which windows came back full, which failed
and which were never reached. They are those of the named run or, for a window, of every finished run of the source
whose range meets it. For one interval of one query the latest run's receipt counts: a window one run did not reach is
read by a later one.
"""

from biz_rules.history_models import HistoryError, Interval, WindowReceipt
from biz_rules.timestamps import parse_timestamp

MAX_RUNS = 200
MAX_RECEIPTS = 2000


def _meets(receipt, window):
    """Whether the receipt's window overlaps [start, end). A read without bounds cannot be placed in time and may hold
    anything, so it always meets."""
    if window is None or receipt.start is None or receipt.end is None:
        return True
    return parse_timestamp(receipt.start) < window[1] and parse_timestamp(receipt.end) > window[0]


def effective_receipts(runs, window=None):
    """(receipts, truncated intervals) of the reads in `runs` that meet the window (all of them without one).
    runs: (query, coverage) pairs, oldest first."""
    latest = {}
    for query, coverage in runs:
        for stored in coverage.get("window_coverage") or []:
            receipt = WindowReceipt.model_validate(stored)
            if _meets(receipt, window):
                latest[(query, receipt.start, receipt.end)] = receipt
    if len(latest) > MAX_RECEIPTS:
        raise HistoryError("window_too_wide", "窗口内的读取回执超过 %d 条，请缩小窗口" % MAX_RECEIPTS, 422)
    receipts = list(latest.values())
    return receipts, [Interval(start=r.start, end=r.end) for r in receipts if r.possibly_truncated]
