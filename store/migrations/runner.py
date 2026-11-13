"""Database migrations: run the versions/V*.sql files a database has not run yet, in order.

    python -m store.migrations.runner --db {app|history|publication} --plan    # list the files still to run
    python -m store.migrations.runner --db {app|history|publication} --apply   # run them

app is the object library, history the History library, publication the original collection library with the
publication outbox; files_for() says which file is whose. Each file runs in one transaction with its ledger row. The
ledgers are the tables the earlier runners wrote, so nothing they ran is run again:
- history, publication: fabric_schema_ledger_events; a version has run when its latest event is applied,
  observed_baseline or compat_mapped (the last two: taken over by the former --adopt-baseline);
- app: schema_migrations (+ schema_migrations_shared_slot); a file has run when a row has its description (the team
  line ran today's V020 and V033-V035 under other numbers, with the same descriptions).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import re
import sys
from pathlib import Path

from loguru import logger
from sqlalchemy import text

VERSIONS = Path(__file__).parent / "versions"
EVENTS = "fabric_schema_ledger_events"
LOCK_KEY = 0x4661627269634D67  # the advisory lock the former runner took too: two runs never overlap
ENGINES = {"app": ("graph.db.session", "get_engine"), "history": ("store.history", "history_engine"),
           "publication": ("graph.publish.publication", "publication_engine")}
# there while the role's ledger is empty: the database was built without this ledger (before it, or by the app role,
# which runs V019 and V026 too); its files are not run again
FIRST_TABLE = {"history": "history_sources", "publication": "publication_outbox"}
EVENT_LEDGER = (f"CREATE TABLE IF NOT EXISTS {EVENTS} (event_id bigserial PRIMARY KEY, db_role text NOT NULL, "
                "version text NOT NULL, file text NOT NULL, event text NOT NULL, checksum_md5 text NOT NULL, "
                "sha256 text NOT NULL, evidence jsonb NOT NULL DEFAULT '{}'::jsonb, actor text NOT NULL, "
                "at timestamptz NOT NULL DEFAULT clock_timestamp())")
APP_LEDGER = ("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, description TEXT DEFAULT '', "
              "checksum TEXT DEFAULT '', applied_at TIMESTAMPTZ DEFAULT NOW())")
SHARED_SLOT = ("CREATE TABLE IF NOT EXISTS schema_migrations_shared_slot (version TEXT PRIMARY KEY, description "
               "TEXT NOT NULL, checksum TEXT NOT NULL, slot_held_by TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL "
               "DEFAULT NOW())")
# V008-V011 carry their own BEGIN; and COMMIT; lines: left out, so the file runs inside the runner's transaction
OWN_TRANSACTION = re.compile(r"(?im)^[ \t]*(BEGIN|COMMIT)[ \t]*;[ \t]*$")


class MigrationError(RuntimeError):
    """The ledger shows something this runner cannot explain; nothing was run."""


def version(path: Path) -> str:
    return path.name.split("__", 1)[0]


def description(path: Path) -> str:
    return path.stem.split("__", 1)[1].replace("_", " ")


def files_for(role: str) -> list[Path]:
    """app: V001-V035. publication: V019, V026. history: V017-V032 without those two, and every file after V035."""
    def belongs(n: int) -> bool:
        if role == "app":
            return n <= 35
        if role == "publication":
            return n in (19, 26)
        return n >= 17 and n not in (19, 26, 33, 34, 35)

    return sorted(p for p in VERSIONS.glob("V*.sql") if belongs(int(version(p)[1:])))


def has_table(conn, name: str) -> bool:
    return conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar_one()


def done(conn, role: str) -> set[str]:
    """The names of the role's files its ledger shows as run."""
    files = files_for(role)
    if role == "app":
        tables = [t for t in ("schema_migrations", "schema_migrations_shared_slot") if has_table(conn, t)]
        recorded = {d for t in tables for d in conn.execute(text(f"SELECT description FROM {t}")).scalars()}
        unknown = recorded - {description(p) for p in files}
        if unknown:
            raise MigrationError(f"schema_migrations has rows that no file today stands for: {sorted(unknown)}")
        return {p.name for p in files if description(p) in recorded}
    latest = {}  # version -> its latest event
    if has_table(conn, EVENTS):
        latest = dict(conn.execute(text(f"SELECT DISTINCT ON (version) version, event FROM {EVENTS} "
                                        "WHERE db_role = :r ORDER BY version, event_id DESC"), {"r": role}).all())
    if "baseline_revoked" in latest.values():
        raise MigrationError(f"a baseline in the {role} ledger was revoked: a person decides what the database has")
    if not latest and has_table(conn, FIRST_TABLE[role]):
        raise MigrationError(f"{FIRST_TABLE[role]} exists but the {role} ledger is empty: the database was built "
                             "without this ledger; record what it has by hand, its files are not run again")
    return {p.name for p in files if version(p) in latest}


def plan(role: str, engine) -> list[str]:
    """The files --apply would run. Reads only."""
    with engine.connect() as conn:
        finished = done(conn, role)
    return [p.name for p in files_for(role) if p.name not in finished]


def run_file(conn, role: str, path: Path) -> None:
    """Run one file and write its ledger row, in the caller's transaction."""
    sql = path.read_text(encoding="utf-8")
    conn.exec_driver_sql("SET LOCAL lock_timeout = '3s'")  # give up rather than queue behind live readers
    conn.exec_driver_sql(OWN_TRANSACTION.sub("", sql))  # the whole file: PostgreSQL parses it itself
    row = {"r": role, "v": version(path), "d": description(path), "f": path.name,
           "md5": hashlib.md5(sql.encode()).hexdigest(), "sha": hashlib.sha256(sql.encode()).hexdigest()}
    if role != "app":
        conn.exec_driver_sql(EVENT_LEDGER)
        conn.execute(text(f"INSERT INTO {EVENTS} (db_role, version, file, event, checksum_md5, sha256, actor) "
                          "VALUES (:r, :v, :f, 'applied', :md5, :sha, 'runner')"), row)
        return
    conn.exec_driver_sql(APP_LEDGER)
    held = conn.execute(text("SELECT description FROM schema_migrations WHERE version = :v"), row).scalar()
    if held is None:
        conn.execute(text("INSERT INTO schema_migrations (version, description, checksum) VALUES (:v, :d, :md5)"), row)
    else:  # the team line's row holds this number (its V021 is today's V034): this file is recorded beside it
        conn.exec_driver_sql(SHARED_SLOT)
        conn.execute(text("INSERT INTO schema_migrations_shared_slot (version, description, checksum, slot_held_by) "
                          "VALUES (:v, :d, :md5, :held)"), {**row, "held": held})


def apply(role: str, engine) -> list[str]:
    """Run the pending files in order, each in its own transaction with its ledger row; returns their names."""
    ran = []
    for path in files_for(role):
        with engine.begin() as conn:
            # held until this transaction ends: a second run waits here, then sees what this one recorded
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
            if path.name in done(conn, role):
                continue
            logger.info(f"[Migration] 执行: {path.name}")
            run_file(conn, role, path)
        ran.append(path.name)
    if role == "history":
        seed_history_sources(engine)
    return ran


def seed_history_sources(engine) -> None:
    """A history_sources row for each catalog source (fetch.catalog.source_catalog) that has none; an existing row is
    the administrator's decision (read_enabled) and stays as it is."""
    from fetch.sources import SOURCE_CATALOG
    from store.history_models import canonical

    with engine.begin() as conn:
        for source_id, catalog in SOURCE_CATALOG.items():
            conn.execute(text("INSERT INTO history_sources (source_id, catalog) VALUES (:id, CAST(:catalog AS jsonb)) "
                              "ON CONFLICT (source_id) DO NOTHING"), {"id": source_id, "catalog": canonical(catalog)})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m store.migrations.runner", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, choices=sorted(ENGINES))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true", help="list the files still to run; writes nothing")
    action.add_argument("--apply", action="store_true", help="run them")
    args = parser.parse_args(argv)
    module, factory = ENGINES[args.db]
    engine = getattr(importlib.import_module(module), factory)()
    try:
        names = apply(args.db, engine) if args.apply else plan(args.db, engine)
    except MigrationError as exc:
        print(f"refused, nothing was run: {exc}")
        return 2
    print(("ran: " if args.apply else "pending: ") + (" ".join(names) or "nothing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
