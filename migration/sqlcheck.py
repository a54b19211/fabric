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


def mask(sql: str) -> tuple[str, str | None]:
    """The text with comments and the insides of quotes and dollar-quoted blocks blanked (same length), and what is
    left open at the end of the file, if anything."""
    out, i, n = [], 0, len(sql)

    def blank(start, end):
        out.append("".join(c if c == "\n" else " " for c in sql[start:end]))

    while i < n:
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            if depth:
                return "".join(out), "block comment"
            i = j
        elif sql[i] in "'\"":
            quote, j = sql[i], i + 1
            while j < n and not (sql[j] == quote and not sql.startswith(quote * 2, j)):
                j += 2 if sql.startswith(quote * 2, j) else 1
            if j >= n:
                return "".join(out), "single quote" if quote == "'" else "double quote"
            blank(i, j + 1)
            i = j + 1
        elif sql[i] == "$" and _DOLLAR.match(sql, i):
            tag = _DOLLAR.match(sql, i).group(0)
            end = sql.find(tag, i + len(tag))
            if end == -1:
                return "".join(out), "dollar quote " + tag
            blank(i, end + len(tag))
            i = end + len(tag)
        else:
            out.append(sql[i])
            i += 1
    return "".join(out), None


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
