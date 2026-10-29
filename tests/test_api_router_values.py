"""API display mappings retain falsy values without changing native source fields."""

import copy
import json

import pytest

from core_engine import api_router


@pytest.mark.parametrize(
    "value, expected",
    [(0, "0"), (0.0, "0.0"), (False, "False"), (True, "True"),
     (None, ""), ("", ""), ("零", "零")],
)
def test_text_mapping_preserves_zero_and_false(value, expected):
    record = {"nested": {"items": [value]}}
    original = copy.deepcopy(record)
    path = "nested.items.0"
    assert api_router._get_nested(record, path) == expected
    assert api_router._extract_extra_value(record, path) == expected
    assert api_router._extract_extra_value(record, {"path": path, "type": "str"}) == expected
    assert record == original
    assert type(record["nested"]["items"][0]) is type(value)


def test_missing_null_and_empty_remain_legacy_empty_display_not_native_facts():
    record = {"null": None, "empty": "", "zero": 0, "false": False}
    for path in ["absent", "null", "empty", "null.child", "empty.child"]:
        assert api_router._get_nested(record, path) == ""
        assert api_router._extract_extra_value(record, path) == ""
    assert "absent" not in record
    assert record["null"] is None and record["empty"] == ""
    assert type(record["zero"]) is int and record["false"] is False
    # The existing source rendering keeps keys and raw values, not mapped placeholders.
    markdown = api_router._record_to_markdown(record, "Synthetic")
    assert "**zero**: 0" in markdown and "**false**: False" in markdown
    assert "**null**: None" in markdown and "**empty**: " in markdown
    assert "**absent**" not in markdown


@pytest.mark.parametrize("path", ["items.9", "items.bad", "items.0.child", "unknown"])
def test_invalid_paths_keep_display_contract(path):
    record = {"items": [False]}
    assert api_router._get_nested(record, path) == ""
    assert api_router._extract_extra_value(record, path) == ""


@pytest.mark.parametrize("value, expected", [(0, 0), ("0", 0), (False, None), (None, None), ("", None)])
def test_existing_explicit_integer_mapping_is_unchanged(value, expected):
    assert api_router._extract_extra_value(
        {"count": value}, {"path": "count", "type": "int"}
    ) == expected


def test_keyed_list_mapping_keeps_zero_false_and_excludes_only_empty_values():
    record = {"items": [{"v": 0}, {"v": False}, {"v": None}, {"v": ""}, {}, "bad", {"v": "ok"}]}
    assert api_router._extract_extra_value(
        record, {"path": "items", "type": "list", "key": "v"}
    ) == ["0", "False", "ok"]


def test_real_router_keeps_distinct_ids_and_typed_nested_originals(monkeypatch):
    source = [
        {"id": 0, "title": 0, "flag": False, "nested": {"null": None, "empty": ""}},
        {"id": False, "title": "next", "flag": False},
    ]
    original = copy.deepcopy(source)
    monkeypatch.setattr(api_router, "iter_api_records", lambda _profile: iter(source))
    profile = {
        "uid_field": "id",
        "field_map": {"title": "title", "sniffed_meta.flag": "flag"},
        "extra_fields": {"count": {"path": "id", "type": "int"}, "flag": "flag"},
    }
    rows = list(api_router.iter_api_results("synthetic", profile))
    assert rows[0]["uuid"] != rows[1]["uuid"]
    assert rows[0]["title"] == "0"
    assert rows[0]["extra"] == {"count": 0, "flag": "False"}
    assert rows[1]["extra"]["count"] is None
    assert rows[0]["sniffed_meta"]["flag"] == "False"
    assert '**nested**: ' + json.dumps(source[0]["nested"], ensure_ascii=False) in rows[0]["markdown"]
    assert source == original
