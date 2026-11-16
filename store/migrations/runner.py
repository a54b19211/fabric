"""Database migrations: run the versions/V*.sql files a database has not run yet, in order.

    python -m store.migrations.runner --db fabric --plan    # list the files still to run
    python -m store.migrations.runner --db fabric --apply   # run them

fabric is the fabric library (资料库); files_for() says which file is its. Each file runs in one transaction with its
ledger row. The ledger is fabric_schema_ledger_events, the table the earlier runners wrote, so nothing they ran is run
again; a version has run when its latest event is applied, observed_baseline or compat_mapped (the last two: taken
over by the former --adopt-baseline).
V001-V016, V019, V026 and V033-V035 belonged to the graph's object library and the collection library's publication
outbox (the graph is now its own project); they stay here as the record of what those databases ran.
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
ENGINES = {"fabric": ("store.records", "fabric_engine")}
# there while the role's ledger is empty: the database was built without this ledger; its files are not run again
FIRST_TABLE = {"fabric": "fabric_sources"}
# Until V046 the fabric library was called "history": its ledger rows were written under db_role 'history' and its
# first table was history_sources. A database that has not run V046 yet still has both; done() reads them as the
# fabric role's, and V046 renames the tables and relabels the rows.
BEFORE_V046 = {"fabric": ("history", "history_sources")}
EVENT_LEDGER = (f"CREATE TABLE IF NOT EXISTS {EVENTS} (event_id bigserial PRIMARY KEY, db_role text NOT NULL, "
                "version text NOT NULL, file text NOT NULL, event text NOT NULL, checksum_md5 text NOT NULL, "
                "sha256 text NOT NULL, evidence jsonb NOT NULL DEFAULT '{}'::jsonb, actor text NOT NULL, "
                "at timestamptz NOT NULL DEFAULT clock_timestamp())")
# V008-V011 carry their own BEGIN; and COMMIT; lines: left out, so the file runs inside the runner's transaction
OWN_TRANSACTION = re.compile(r"(?im)^[ \t]*(BEGIN|COMMIT)[ \t]*;[ \t]*$")


class MigrationError(RuntimeError):
    """The ledger shows something this runner cannot explain; nothing was run."""


def version(path: Path) -> str:
    return path.name.split("__", 1)[0]


def files_for(role: str) -> list[Path]:
    """fabric: V017-V032 without V019 and V026, and every file after V035."""
    def belongs(n: int) -> bool:
        return n >= 17 and n not in (19, 26, 33, 34, 35)

    return sorted(p for p in VERSIONS.glob("V*.sql") if belongs(int(version(p)[1:])))


def has_table(conn, name: str) -> bool:
    return conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar_one()


def done(conn, role: str) -> set[str]:
    """The names of the role's files its ledger shows as run."""
    files = files_for(role)
    old_role, old_first = BEFORE_V046.get(role, (role, FIRST_TABLE[role]))
    latest = {}  # version -> its latest event
    if has_table(conn, EVENTS):
        latest = dict(conn.execute(text(f"SELECT DISTINCT ON (version) version, event FROM {EVENTS} "
                                        "WHERE db_role IN (:r, :old) ORDER BY version, event_id DESC"),
                                   {"r": role, "old": old_role}).all())
    if "baseline_revoked" in latest.values():
        raise MigrationError(f"a baseline in the {role} ledger was revoked: a person decides what the database has")
    built = next((t for t in (FIRST_TABLE[role], old_first) if has_table(conn, t)), None)
    if not latest and built:
        raise MigrationError(f"{built} exists but the {role} ledger is empty: the database was built "
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
    row = {"r": role, "v": version(path), "f": path.name,
           "md5": hashlib.md5(sql.encode()).hexdigest(), "sha": hashlib.sha256(sql.encode()).hexdigest()}
    conn.exec_driver_sql(EVENT_LEDGER)
    conn.execute(text(f"INSERT INTO {EVENTS} (db_role, version, file, event, checksum_md5, sha256, actor) "
                      "VALUES (:r, :v, :f, 'applied', :md5, :sha, 'runner')"), row)


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
    if role == "fabric":
        seed_sources(engine)
    return ran


def seed_sources(engine) -> None:
    """A fabric_sources row for each catalog source (fetch.catalog.source_catalog) that has none (readable unless it is
    in DEFAULT_CLOSED); an existing row is the administrator's decision (read_enabled, fetch_enabled) and stays."""
    from fetch.catalog import DEFAULT_CLOSED
    from fetch.sources import SOURCE_CATALOG
    from store.record_models import canonical

    with engine.begin() as conn:
        for source_id, catalog in SOURCE_CATALOG.items():
            conn.execute(text("INSERT INTO fabric_sources (source_id, catalog, read_enabled) "
                              "VALUES (:id, CAST(:catalog AS jsonb), :ok) ON CONFLICT (source_id) DO NOTHING"),
                         {"id": source_id, "catalog": canonical(catalog), "ok": source_id not in DEFAULT_CLOSED})


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
