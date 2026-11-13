"""The object library migration ledger: schema_migrations and the runner tables beside it, and how a recorded row is
recognised (store.migrations.known_migrations). Reading never writes; rows are never deleted or rewritten."""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

MIGRATION_TABLE = "schema_migrations"


def _ensure_migration_table(conn):
    conn.execute(__import__("sqlalchemy").text(f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            version     TEXT        PRIMARY KEY,
            description TEXT        DEFAULT '',
            checksum    TEXT        DEFAULT '',
            applied_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    conn.commit()


def _checksum(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def _parse_filename(path: Path) -> tuple[str, str]:
    """从文件名解析版本号和描述，如 V001__init_tables.sql → ('V001', 'init tables')"""
    stem = path.stem  # V001__init_tables
    parts = stem.split("__", 1)
    version = parts[0]
    description = parts[1].replace("_", " ") if len(parts) > 1 else ""
    return version, description


# A migration is identified by what schema_migrations stores for it: version, description and checksum.
# The rebuild line and the team line numbered V020-V023 in parallel, so a version alone does not say which
# migration ran. Every row is looked up in store.migrations.known_migrations (every file the repository ever had):
#   current / earlier  -> today's file of that version has run;
#   renumbered         -> today's file with that content (under its new number) has run; the row stays as it is and
#                         the mapping is recorded in RENUMBERED_TABLE;
#   superseded/unknown -> nothing is run and nothing is written: a person has to say what this database is.
# A file whose version slot in schema_migrations is held by a renumbered row is recorded in SHARED_SLOT_TABLE.
# Rows are never deleted or rewritten.
RENUMBERED_TABLE = "schema_migration_renumbered"
SHARED_SLOT_TABLE = "schema_migrations_shared_slot"


class MigrationStateError(RuntimeError):
    """The recorded migrations do not match any migration of this repository; nothing was run or written."""


@dataclass
class MigrationState:
    applied: set[str] = field(default_factory=set)  # file names that have run
    renumbered: list[tuple] = field(default_factory=list)  # (recorded row, file today)
    held_slots: dict[str, str] = field(default_factory=dict)  # version -> description of the row holding it


def _table_exists(conn, name) -> bool:
    from sqlalchemy import text

    return conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar_one()


def _rows(conn, table, columns):
    from sqlalchemy import text

    if not _table_exists(conn, table):
        return []
    return [tuple(row) for row in conn.execute(text(f"SELECT {columns} FROM {table} ORDER BY 1"))]


def read_state(conn, migrations) -> MigrationState:
    """What has run, from schema_migrations and the runner's own tables. Raises MigrationStateError on any row
    that is not a known migration (or a known one no file today stands for), before anything is written."""
    from store.migrations.known_migrations import KNOWN

    known = {(version, description, checksum): (kind, target, seen)
             for version, description, checksum, kind, target, seen in KNOWN}
    files = {_parse_filename(path)[0]: path for path in migrations}
    state, problems = MigrationState(), []
    for version, description, checksum in _rows(conn, MIGRATION_TABLE, "version, description, checksum"):
        kind, target, seen = known.get((version, description, checksum), (None, None, None))
        if kind in ("current", "earlier"):
            state.applied.add(target)
        elif kind == "renumbered":
            state.applied.add(target)
            state.renumbered.append(((version, description, checksum), target))
            if version in files and files[version].name != target:
                state.held_slots[version] = description
        elif kind == "superseded":
            today = files[version].name if version in files else "no file"
            problems.append(f"{version} '{description}' (checksum {checksum}) is an earlier migration (seen in "
                            f"{seen}) that no file stands for today; today's {version} is {today} and has not run")
        else:
            problems.append(f"{version} '{description}' (checksum {checksum or 'empty'}) is not a migration any "
                            f"commit of this repository had")
    for version, description, checksum in _rows(conn, SHARED_SLOT_TABLE, "version, description, checksum"):
        path = files.get(version)
        if path is None or (version, description, checksum) not in known:
            problems.append(f"{SHARED_SLOT_TABLE} row {version} '{description}' does not match a known migration")
        else:
            state.applied.add(known[(version, description, checksum)][1])
    if problems:
        raise MigrationStateError("schema_migrations 里有无法识别的记录，没有执行也没有写入任何内容：\n- "
                                  + "\n- ".join(problems))
    return state


def _ensure_runner_tables(conn):
    from sqlalchemy import text

    _ensure_migration_table(conn)
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {RENUMBERED_TABLE} (
            recorded_version     TEXT NOT NULL,
            recorded_description TEXT NOT NULL,
            recorded_checksum    TEXT NOT NULL,
            current_file         TEXT NOT NULL,
            recognised_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (recorded_version, recorded_description)
        )"""))
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {SHARED_SLOT_TABLE} (
            version     TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            checksum    TEXT NOT NULL,
            slot_held_by TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""))
    conn.commit()
