"""Timestamps read back from PostgreSQL, on the Python Fabric runs (3.10).

PostgreSQL writes timestamptz values in JSON (`jsonb_build_object(..., clock_timestamp())`) and in `::text`
with 0 to 6 fractional digits - trailing zeros dropped, so 58.058950 becomes "58.05895" - and with a short
offset ("+00") in text output. Python 3.10's `datetime.fromisoformat` accepts only 3 or 6 digits and "+HH:MM",
so a value that happens to end in a zero failed to parse. `parse_timestamp` reads every such value; anything
else goes to `datetime.fromisoformat` unchanged, so no value accepted before is read differently.
"""

from __future__ import annotations

import re
from datetime import datetime

_PG_TIMESTAMP = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?"
    r"(?P<offset>Z|[+-]\d{2}(?::?\d{2})?)?"
)


def parse_timestamp(value: str) -> datetime:
    """A timestamp as PostgreSQL or Python wrote it -> datetime. The offset is kept as given (no offset:
    a naive datetime, as `fromisoformat` returns it); callers that require one still check tzinfo."""
    match = _PG_TIMESTAMP.fullmatch(value.strip())
    if match is None:
        return datetime.fromisoformat(value)
    fraction = (match["fraction"] or "").ljust(6, "0")
    offset = match["offset"] or ""
    if offset == "Z":
        offset = "+00:00"
    elif offset:
        digits = offset[1:].replace(":", "")
        offset = f"{offset[0]}{digits[:2]}:{digits[2:4] or '00'}"
    return datetime.fromisoformat(f"{match['date']}T{match['time']}.{fraction}{offset}")
