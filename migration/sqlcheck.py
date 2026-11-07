"""Checks of a migration file before PostgreSQL executes it. The runner hands the whole file to PostgreSQL (which
parses function bodies, comments and quoting itself); this module only reads it first, to refuse what a ledger
transaction cannot hold and to tell which files change rows."""

from __future__ import annotations

import re
from dataclasses import dataclass

from migration.sqltext import split_sql

_WORD = re.compile(r"[A-Za-z_]+")
_DOLLAR = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
# statements that change rows (or may), as opposed to structure
DATA_STATEMENTS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "TRUNCATE", "DO", "CALL", "SELECT", "WITH"})
TRANSACTION_CONTROL = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "START", "END", "ABORT", "SAVEPOINT", "RELEASE"})


def _line_comment(sql: str, i: int) -> tuple[int, str | None]:
    end = sql.find("\n", i)
    return (len(sql) if end == -1 else end), None


def _block_comment(sql: str, i: int) -> tuple[int, str | None]:
    depth, j = 1, i + 2
    while j < len(sql) and depth:
        if sql.startswith("/*", j):
            depth, j = depth + 1, j + 2
        elif sql.startswith("*/", j):
            depth, j = depth - 1, j + 2
        else:
            j += 1
    return j, ("block comment" if depth else None)


def _quoted(sql: str, i: int) -> tuple[int, str | None]:
    quote, j = sql[i], i + 1
    while j < len(sql) and not (sql[j] == quote and not sql.startswith(quote * 2, j)):
        j += 2 if sql.startswith(quote * 2, j) else 1
    if j >= len(sql):
        return len(sql), "single quote" if quote == "'" else "double quote"
    return j + 1, None


def _dollar_quoted(sql: str, i: int) -> tuple[int, str | None]:
    tag = _DOLLAR.match(sql, i).group(0)
    end = sql.find(tag, i + len(tag))
    return (len(sql), "dollar quote " + tag) if end == -1 else (end + len(tag), None)


def _span(sql: str, i: int):
    """The reader for the text that starts at i if it is a comment, quote or dollar quote; None for ordinary text."""
    if sql.startswith("--", i):
        return _line_comment
    if sql.startswith("/*", i):
        return _block_comment
    if sql[i] in "'\"":
        return _quoted
    if sql[i] == "$" and _DOLLAR.match(sql, i):
        return _dollar_quoted
    return None


def mask(sql: str) -> tuple[str, str | None]:
    """The text with comments and the insides of quotes and dollar-quoted blocks blanked (same length), and what is
    left open at the end of the file, if anything."""
    out, i = [], 0
    while i < len(sql):
        reader = _span(sql, i)
        if reader is None:
            out.append(sql[i])
            i += 1
            continue
        end, open_at = reader(sql, i)
        out.append("".join(c if c == "\n" else " " for c in sql[i:end]))
        if open_at:
            return "".join(out), open_at
        i = end
    return "".join(out), None


_CONTROL_LINE = re.compile(r"(?im)^[ \t]*(?:BEGIN|COMMIT)[ \t]*;")


def neutralise_transaction_control(sql: str) -> str:
    """The text to execute for a file that carries its own top-level `BEGIN;` / `COMMIT;` (four object-library files from
    before the ledger): exactly those statements are blanked (same length, the rest byte for byte), so the file runs inside
    the runner's transaction together with its post-condition and its ledger row. The file on disk and its recorded checksum
    are the published ones; this is applied at execution time only. Raises ValueError if anything else of the kind
    remains (ROLLBACK, a second kind of control, one that is not alone on its line)."""
    masked, open_at = mask(sql)
    if open_at:
        raise ValueError("unclosed " + open_at)
    out = list(sql)
    for found in _CONTROL_LINE.finditer(masked):
        for position in range(found.start(), found.end()):
            if out[position] != "\n":
                out[position] = " "
    result = "".join(out)
    if any("top-level" in problem for problem in inspect_sql(result).problems):
        raise ValueError("transaction control remains after neutralising BEGIN/COMMIT")
    return result


@dataclass(frozen=True)
class FileReport:
    statements: int
    problems: tuple[str, ...]
    data_statements: tuple[str, ...]  # first keyword of each statement that may change rows
    checks: tuple[str, ...]


def inspect_sql(sql: str, *, allow_transaction_control: bool = False) -> FileReport:
    """What the runner needs to know before it runs a file as one transaction together with its ledger row."""
    masked, open_at = mask(sql)
    problems = []
    if open_at:
        problems.append(f"unclosed {open_at}")
    data, controls = [], []
    for statement in split_sql(masked):
        words = _WORD.findall(statement)
        if not words:
            continue
        first = words[0].upper()
        if first in TRANSACTION_CONTROL:
            controls.append(first)
        if first in DATA_STATEMENTS:
            data.append(first)
        if "CONCURRENTLY" in {w.upper() for w in words}:
            problems.append("CONCURRENTLY cannot run inside the migration transaction; use an online operation")
    if controls and not allow_transaction_control:
        problems.append("top-level " + ", ".join(sorted(set(controls))) + ": a file must fit in one transaction")
    checks = ("quotes_closed", "no_top_level_txn", "no_concurrently")
    return FileReport(len(split_sql(sql)), tuple(problems), tuple(data), checks)
