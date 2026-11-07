"""The top-level statement splitter on its own cases and on every migration file (golden: statement count and
digest of the split, produced by the splitter as it was before it moved to migration.sqltext)."""

import hashlib
import json
from pathlib import Path

import pytest

from migration import migrate

VERSIONS = Path(migrate.__file__).parent / "versions"
GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "sql_split_golden.json").read_text())
split = migrate._split_sql


def digest(statements):
    return hashlib.sha256("\n;\n".join(statements).encode()).hexdigest()


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_every_migration_file_splits_as_it_did(name):
    statements = split((VERSIONS / name).read_text(encoding="utf-8"))
    assert {"statements": len(statements), "sha256": digest(statements)} == GOLDEN[name]


def test_the_golden_covers_exactly_the_files_that_exist():
    assert sorted(GOLDEN) == sorted(p.name for p in VERSIONS.glob("V*.sql") if int(p.name[1:4]) <= 35)


@pytest.mark.parametrize("sql,expected", [
    ("SELECT 1; SELECT 2;", ["SELECT 1", "SELECT 2"]),
    ("SELECT 'a;b'; SELECT \"c;d\";", ["SELECT 'a;b'", 'SELECT "c;d"']),
    ("SELECT 'it''s;'; SELECT 2", ["SELECT 'it''s;'", "SELECT 2"]),
    ("CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql; SELECT 2;",
     ["CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql", "SELECT 2"]),
    ("SELECT $tag$ a;b $tag$; SELECT 2;", ["SELECT $tag$ a;b $tag$", "SELECT 2"]),
    ("SELECT 1; -- not; a split\nSELECT 2;", ["SELECT 1", "-- not; a split\nSELECT 2"]),
    ("SELECT /* a; /* nested; */ b; */ 1; SELECT 2;", ["SELECT /* a; /* nested; */ b; */ 1", "SELECT 2"]),
    ("", []),
    (";;;", []),
])
def test_the_splitter_cuts_only_at_top_level_semicolons(sql, expected):
    assert split(sql) == expected


def test_an_unclosed_dollar_quote_is_returned_whole_not_swallowed():
    assert split("SELECT 1; SELECT $x$ open; more") == ["SELECT 1", "SELECT $x$ open; more"]


def test_a_plain_semicolon_split_would_cut_the_function_body_of_v019():
    sql = (VERSIONS / "V019__publication_outbox.sql").read_text(encoding="utf-8")
    assert len(sql.split(";")) > len(split(sql)) + 20  # the reason the old history/index runners could not take V019
