"""parse_timestamp reads PostgreSQL's variable-length fractional seconds and short offsets (Python 3.10's
fromisoformat takes only 3 or 6 digits): the source-health heartbeat failed on a value ending in a zero."""

from datetime import datetime, timedelta, timezone

import pytest

from store.timestamps import parse_timestamp

UTC = timezone.utc
BASE = datetime(2026, 11, 6, 9, 42, 58, tzinfo=UTC)


@pytest.mark.parametrize(
    "text, micro",
    [
        ("2026-11-06T09:42:58+00:00", 0),
        ("2026-11-06T09:42:58.0+00:00", 0),
        ("2026-11-06T09:42:58.1+00:00", 100000),
        ("2026-11-06T09:42:58.058+00:00", 58000),
        ("2026-11-06T09:42:58.05895+00:00", 58950),  # the value that failed (r208)
        ("2026-11-06T09:42:58.058950+00:00", 58950),
        ("2026-11-06T09:42:58.123456+00:00", 123456),
    ],
)
def test_fraction_of_any_length_up_to_six_digits(text, micro):
    assert parse_timestamp(text) == BASE + timedelta(microseconds=micro)


@pytest.mark.parametrize(
    "text, offset",
    [
        ("2026-11-06T09:42:58.05895Z", timedelta(0)),
        ("2026-11-06 09:42:58.05895+00", timedelta(0)),  # PostgreSQL ::text output
        ("2026-11-06T09:42:58.05895+08:00", timedelta(hours=8)),
        ("2026-11-06T09:42:58.05895+0800", timedelta(hours=8)),
        ("2026-11-06T09:42:58.05895-05:30", -timedelta(hours=5, minutes=30)),
        ("2026-11-06T09:42:58.5-07", -timedelta(hours=7)),
    ],
)
def test_offsets_are_kept(text, offset):
    value = parse_timestamp(text)
    assert value.utcoffset() == offset
    assert value.replace(tzinfo=None, microsecond=0) == datetime(2026, 11, 6, 9, 42, 58)
    assert value.microsecond in (58950, 500000)


def test_same_instant_whatever_the_digits():
    assert parse_timestamp("2026-11-06T17:42:58.05895+08:00") == parse_timestamp("2026-11-06T09:42:58.058950Z")


def test_no_offset_stays_naive_as_fromisoformat_returns_it():
    assert parse_timestamp("2026-11-06T09:42:58.05895").tzinfo is None


def test_staleness_arithmetic_with_a_five_digit_heartbeat():
    observed = datetime(2026, 11, 6, 9, 45, 0, tzinfo=UTC)
    assert (observed - parse_timestamp("2026-11-06T09:42:58.05895+00:00")).total_seconds() == pytest.approx(121.94105)


@pytest.mark.parametrize("text", ["2026-11-06", "2026-11-06T09:42", "2026-11-06T09:42:58.123+00:00"])
def test_other_values_read_as_fromisoformat_reads_them(text):
    assert parse_timestamp(text) == datetime.fromisoformat(text)


@pytest.mark.parametrize("text", ["", "yesterday", "2026-13-06T09:42:58+00:00"])
def test_what_is_no_timestamp_still_raises(text):
    with pytest.raises(ValueError):
        parse_timestamp(text)


def test_seven_fraction_digits_are_cut_to_microseconds():
    # Python 3.11+ reads a 7-digit fraction (some .NET-style APIs send it) and drops the last digit
    assert parse_timestamp("2026-11-06T09:42:58.1234567+00:00").microsecond == 123456
