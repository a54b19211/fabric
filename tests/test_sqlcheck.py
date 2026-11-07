"""The pre-execution checks of a migration file (migration.sqlcheck)."""

import pytest

from migration.sqlcheck import inspect_sql, mask


def test_a_plain_file_passes_with_all_three_checks_named():
    report = inspect_sql("CREATE TABLE t(a int);\nCREATE INDEX i ON t(a);")
    assert report.problems == () and report.statements == 2 and report.data_statements == ()
    assert report.checks == ("quotes_closed", "no_top_level_txn", "no_concurrently")


@pytest.mark.parametrize("sql,word", [
    ("BEGIN; CREATE TABLE t(a int); COMMIT;", "BEGIN"),
    ("CREATE TABLE t(a int); COMMIT;", "COMMIT"),
    ("ROLLBACK;", "ROLLBACK"),
    ("START TRANSACTION; SELECT 1;", "START"),
])
def test_top_level_transaction_control_is_refused(sql, word):
    report = inspect_sql(sql)
    assert any("top-level" in p and word in p for p in report.problems)
    assert inspect_sql(sql, allow_transaction_control=True).problems == ()


def test_transaction_words_inside_a_function_body_comment_or_string_are_not_control():
    sql = ("CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; COMMIT; END; $$ LANGUAGE plpgsql;\n"
           "-- COMMIT; ROLLBACK;\nCOMMENT ON TABLE t IS 'BEGIN; COMMIT;';")
    assert inspect_sql(sql).problems == ()


def test_concurrently_is_refused_but_not_in_a_comment_or_literal():
    assert any("CONCURRENTLY" in p for p in inspect_sql("CREATE INDEX CONCURRENTLY i ON t(a);").problems)
    assert inspect_sql("-- CREATE INDEX CONCURRENTLY\nSELECT 'concurrently';").problems == ()


@pytest.mark.parametrize("sql,what", [
    ("SELECT 'open", "single quote"),
    ('SELECT "open', "double quote"),
    ("SELECT $x$ open", "dollar quote $x$"),
    ("SELECT 1; /* open", "block comment"),
])
def test_an_unclosed_quote_or_comment_is_reported_by_name(sql, what):
    assert inspect_sql(sql).problems == ("unclosed " + what,)


def test_data_statements_are_listed_by_their_first_word():
    sql = "CREATE TABLE t(a int);\nINSERT INTO t VALUES (1);\nUPDATE t SET a=2;\nDO $$ BEGIN END $$;"
    assert inspect_sql(sql).data_statements == ("INSERT", "UPDATE", "DO")


def test_mask_keeps_the_length_and_the_line_structure():
    sql = "SELECT 'a;b' -- c\n, $$x;y$$ /* z */;"
    masked, open_at = mask(sql)
    assert open_at is None and len(masked) == len(sql) and masked.count("\n") == sql.count("\n")
    assert ";" not in masked[:-1]
