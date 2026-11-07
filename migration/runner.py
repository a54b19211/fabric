"""The one migration runner of the Fabric databases.

    python -m migration.runner --db {app|history|publication} --plan             # read only; creates nothing
    python -m migration.runner --db {app|history|publication} --apply            # run what is pending
    python -m migration.runner --db {history|publication} --adopt-baseline       # see migration.baseline
    python -m migration.runner --db history --revoke-baseline V020 --reason ...  # append a correction event
    python -m migration.runner --db publication --online site-index [--drop]     # CONCURRENTLY operations

Each database role keeps its own ledger (the object library: schema_migrations as before, plus the sha256 in the
event table; History and publication: the append-only event table). A file is handed to PostgreSQL whole, in one
transaction together with its ledger row; the statement splitter only reads it first (migration.sqlcheck). Anything
the ledger or the database shows that this repository cannot explain is reported and nothing is run or written.
One advisory lock per physical database covers a whole run, from before the ledger tables exist.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import text

from migration import ledger
from migration.app_ledger import MigrationStateError
from migration.manifests import MANIFESTS, POSTCONDITIONS, TRANSACTION_CONTROL_ALLOWED, VERSIONS_DIR, Manifest
from migration.sqlcheck import FileReport, inspect_sql

RUNNER_VERSION = "fabric.migration.runner.v1"
# "FabricMg" in ASCII: one constant, not a runtime hash, so every runner and every role contends for the same lock
LOCK_KEY = 0x4661627269634D67
ENGINE_FACTORIES = {
    "app": ("db.session", "get_engine"),
    "history": ("db.history", "history_engine"),
    "publication": ("db.publication", "publication_engine"),
}
OBSERVED_MEANING = ("the structure observed on the recorded date equals a fresh install of the files up to this one; "
                    "it does not say the file was ever executed")


class MigrationBusy(MigrationStateError):
    """Another migration holds the database lock; nothing was read or written."""


@dataclass
class Pending:
    file: str
    sql: str
    report: FileReport

    def describe(self) -> dict:
        return {"version": self.file.split("__", 1)[0], "file": self.file, "md5": ledger.md5(self.sql),
                "sha256": ledger.sha256(self.sql), "statements": self.report.statements,
                "data_statements": len(self.report.data_statements), "checks": list(self.report.checks)}


@dataclass
class Observation:
    role: str
    ledger_present: bool = False
    applied: dict[str, dict] = field(default_factory=dict)  # file -> {how, ...}
    problems: list[str] = field(default_factory=list)
    app_state: object = None  # the object library's MigrationState
    error: Exception | None = None  # the object library's own refusal, raised as it is


def default_actor() -> str:
    try:
        return "runner:" + getpass.getuser()
    except Exception:  # noqa: BLE001 - no user database entry (containers)
        return "runner:unknown"


def engine_for(role: str):
    module, name = ENGINE_FACTORIES[role]
    return getattr(importlib.import_module(module), name)()


def file_text(name: str) -> str:
    return (VERSIONS_DIR / name).read_text(encoding="utf-8")


@contextmanager
def migration_lock(engine):
    """A session advisory lock on a connection of its own (never returned to the pool), held for a whole run."""
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar_one():
            raise MigrationBusy("another migration is running on this database (advisory lock held); "
                                "nothing was read or written")
        try:
            yield
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            except Exception:  # noqa: BLE001 - the connection is closed below, which releases a session lock
                logger.warning("[Migration] advisory unlock failed; closing the connection releases it")
    finally:
        conn.invalidate()
        conn.close()


# ---- what the ledger and the database show ----

def _observe_app(conn, manifest: Manifest) -> Observation:
    from migration.app_ledger import MIGRATION_TABLE, read_state

    paths = [VERSIONS_DIR / name for name in manifest.files]
    obs = Observation("app", ledger_present=ledger.table_exists(conn, MIGRATION_TABLE))
    try:
        obs.app_state = read_state(conn, paths)
    except MigrationStateError as exc:
        obs.problems.append(str(exc))
        obs.error = exc
        return obs
    obs.applied = {name: {"how": "applied"} for name in obs.app_state.applied if name in manifest.files}
    return obs


def _check_event_rows(manifest: Manifest, state: ledger.LedgerState) -> list[str]:
    problems = []
    for name, entry in state.present.items():
        if name not in manifest.files:
            problems.append(f"the ledger has {name}, which the {manifest.role} manifest does not list")
        elif entry.checksum_md5 != ledger.md5(file_text(name)) and entry.checksum_md5 not in ledger.known_checksums(name):
            problems.append(f"{name} was recorded with checksum {entry.checksum_md5}, which is no content this file "
                            "ever had in this repository")
    for name in state.revoked:
        problems.append(f"the baseline of {name} was revoked: a named compatibility mapping or a fix migration is "
                        "needed before the ledger is complete again")
    order = [name for name in manifest.files if name in state.present]
    missing = [name for name in manifest.files if name not in state.present]
    if missing and order and manifest.files.index(order[-1]) > manifest.files.index(missing[0]):
        problems.append(f"{order[-1]} is recorded while {missing[0]} before it is not")
    return problems


def _observe_events(conn, role: str, manifest: Manifest) -> Observation:
    state = ledger.read_current(conn, role)
    obs = Observation(role, ledger_present=state.table)
    obs.problems = _check_event_rows(manifest, state)
    for name, entry in state.present.items():
        obs.applied[name] = {"how": entry.event, "at": str(entry.at), "evidence": entry.evidence}
    if (not obs.problems and not state.present and not state.revoked and manifest.sentinel_table
            and ledger.table_exists(conn, manifest.sentinel_table)):
        obs.problems.append(f"{manifest.sentinel_table} exists but the ledger has no record for the {role} role: the "
                            "database was migrated before the ledger; take it over with --adopt-baseline, do not "
                            "run the files again")
    return obs


def observe(conn, role: str) -> Observation:
    manifest = MANIFESTS[role]
    return _observe_app(conn, manifest) if role == "app" else _observe_events(conn, role, manifest)


def pending_files(role: str, applied: dict) -> tuple[list[Pending], str | None]:
    """The files still to run, in order, up to the first one a check refuses (that one's problems are returned)."""
    manifest, runnable = MANIFESTS[role], []
    for name in manifest.files:
        if name in applied:
            continue
        sql = file_text(name)
        report = inspect_sql(sql, allow_transaction_control=name in TRANSACTION_CONTROL_ALLOWED)
        if report.problems:
            return runnable, f"{name}: " + "; ".join(report.problems)
        runnable.append(Pending(name, sql, report))
    return runnable, None


# ---- plan ----

def _receipt(role: str, action: str, obs: Observation, pending: list[Pending], refused: str | None) -> dict:
    state = "refused" if obs.problems or refused else ("ready" if pending else "up_to_date")
    applied = []
    for name, info in sorted(obs.applied.items()):
        row = {"version": name.split("__", 1)[0], "file": name, "how": info["how"]}
        if info["how"] == "observed_baseline":
            row["meaning"] = OBSERVED_MEANING
        applied.append(row)
    return {"runner": RUNNER_VERSION, "db_role": role, "action": action,
            "ledger": "present" if obs.ledger_present else "absent", "state": state, "applied": applied,
            "pending": [p.describe() for p in pending],
            "reason": (obs.problems or [refused])[0] if state == "refused" else None,
            "problems": obs.problems + ([refused] if refused else [])}


def plan(role: str, engine) -> dict:
    """Read only. Creates no table (an absent ledger is reported as such) and writes nothing."""
    with engine.connect() as conn:
        obs = observe(conn, role)
        conn.rollback()
    pending, refused = pending_files(role, obs.applied)
    return _receipt(role, "plan", obs, [] if obs.problems else pending, refused)


# ---- apply ----

def _prepare_ledger(conn, role: str, obs: Observation) -> None:
    ledger.ensure(conn)
    if role == "app":
        from migration.app_ledger import RENUMBERED_TABLE, _ensure_runner_tables

        _ensure_runner_tables(conn)
        for (version, description, checksum), target in obs.app_state.renumbered:
            conn.execute(text(
                f"INSERT INTO {RENUMBERED_TABLE} (recorded_version, recorded_description, recorded_checksum, "
                "current_file) VALUES (:v, :d, :c, :f) ON CONFLICT DO NOTHING"),
                {"v": version, "d": description, "c": checksum, "f": target})
            logger.info(f"[Migration] 识别为改号: {version} {description} = {target}")
    conn.commit()


def _record_app_row(conn, obs: Observation, item: Pending) -> None:
    from datetime import datetime, timezone

    from migration.app_ledger import MIGRATION_TABLE, SHARED_SLOT_TABLE, _parse_filename

    version, description = _parse_filename(VERSIONS_DIR / item.file)
    params = {"v": version, "d": description, "c": ledger.md5(item.sql), "t": datetime.now(timezone.utc)}
    if version in obs.app_state.held_slots:
        conn.execute(text(f"INSERT INTO {SHARED_SLOT_TABLE} (version, description, checksum, slot_held_by, applied_at) "
                          "VALUES (:v, :d, :c, :h, :t)"), {**params, "h": obs.app_state.held_slots[version]})
    else:
        conn.execute(text(f"INSERT INTO {MIGRATION_TABLE} (version, description, checksum, applied_at) "
                          "VALUES (:v, :d, :c, :t)"), params)


def check_postconditions(conn, name: str) -> None:
    for sql in POSTCONDITIONS.get(name, ()):
        if conn.exec_driver_sql(sql).scalar_one() is not True:
            raise MigrationStateError(f"{name}: postcondition not met after the file ran, rolled back: {sql}")


def _run_file(engine, role: str, obs: Observation, item: Pending, actor: str) -> None:
    manifest = MANIFESTS[role]
    logger.info(f"[Migration] 执行: {item.file}")
    with engine.connect() as conn:
        conn.exec_driver_sql(f"SET LOCAL lock_timeout='{manifest.lock_timeout}'")
        if manifest.statement_timeout:
            conn.exec_driver_sql(f"SET LOCAL statement_timeout='{manifest.statement_timeout}'")
        # PostgreSQL parses the whole file (function bodies, comments, quoting); the driver does not rebind colons
        conn.exec_driver_sql(item.sql)
        check_postconditions(conn, item.file)
        if role == "app":
            _record_app_row(conn, obs, item)
        ledger.append(conn, role, item.file, "applied", item.sql, actor=actor,
                      evidence={"runner": RUNNER_VERSION, "checks": list(item.report.checks)})
        conn.commit()
    logger.info(f"[Migration] 完成: {item.file}")


def apply(role: str, engine, *, actor: str | None = None) -> dict:
    """Run what is pending, one file per transaction with its ledger row. Raises MigrationStateError (and has written
    nothing for the refused file) on anything unexplained; files before a refused one are recorded."""
    actor = actor or default_actor()
    with migration_lock(engine):
        with engine.connect() as conn:
            obs = observe(conn, role)
            conn.rollback()
            if obs.error is not None:
                raise obs.error
            if obs.problems:
                raise MigrationStateError("没有执行也没有写入任何内容：\n- " + "\n- ".join(obs.problems))
            pending, refused = pending_files(role, obs.applied)
            if pending or role == "app":  # the object library always recorded renumbered rows it recognised
                _prepare_ledger(conn, role, obs)
        for item in pending:
            _run_file(engine, role, obs, item, actor)
        if refused:
            raise MigrationStateError(f"{refused}; the files before it were run and recorded, it and the later "
                                      "ones were not")
        if not pending:
            logger.info("[Migration] 所有迁移已是最新")
        steps = run_reference_steps(role, engine, actor)
    receipt = _receipt(role, "apply", obs, [], None)
    receipt.update({"state": "applied" if pending else "up_to_date", "ran": [p.describe() for p in pending],
                    "reference_data": steps})
    return receipt


def run_reference_steps(role: str, engine, actor: str) -> list[dict]:
    from migration.reference_data import REFERENCE_STEPS

    results = []
    for name, step in REFERENCE_STEPS.get(role, ()):
        with engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL lock_timeout='3s'")
            result = step(conn)
            if result:
                conn.execute(text(f"INSERT INTO {ledger.REFERENCE_LOG}(db_role, step, result, actor) "
                                  "VALUES (:r, :s, CAST(:x AS jsonb), :a)"),
                             {"r": role, "s": name, "x": json.dumps(result, sort_keys=True), "a": actor})
            results.append({"step": name, "result": result})
    return results


# ---- baseline corrections ----

def revoke_baseline(role: str, engine, version: str, *, reason: str, actor: str | None = None) -> dict:
    """Append a baseline_revoked event for a file the ledger shows as present. The earlier event stays."""
    actor = actor or default_actor()
    manifest = MANIFESTS[role]
    names = [name for name in manifest.files if name.startswith(version + "__")]
    if role == "app" or len(names) != 1:
        raise MigrationStateError(f"no {role} file {version} whose baseline can be revoked")
    with migration_lock(engine):
        with engine.begin() as conn:
            state = ledger.read_current(conn, role)
            entry = state.present.get(names[0])
            if entry is None or entry.event == "applied":
                raise MigrationStateError(f"{names[0]} has no observed_baseline/compat_mapped record to revoke")
            ledger.append_revocation(conn, role, names[0], file_text(names[0]), reason=reason, actor=actor)
    return {"runner": RUNNER_VERSION, "db_role": role, "action": "revoke-baseline", "file": names[0],
            "state": "revoked", "reason": reason}


# ---- command line ----

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m migration.runner", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, choices=sorted(MANIFESTS))
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--plan", action="store_true")
    what.add_argument("--apply", action="store_true")
    what.add_argument("--adopt-baseline", action="store_true")
    what.add_argument("--revoke-baseline", metavar="VERSION")
    what.add_argument("--online", metavar="OPERATION")
    parser.add_argument("--drop", action="store_true", help="with --online: the operation's rollback")
    parser.add_argument("--reason", help="with --revoke-baseline")
    parser.add_argument("--actor", default=None)
    parser.add_argument("--reference-url", help="with --adopt-baseline: an empty throwaway database to fingerprint in "
                                                "(default: a container the runner starts and removes by its ID)")
    return parser


def _dispatch(args) -> dict:
    engine = engine_for(args.db)
    if args.plan:
        return plan(args.db, engine)
    if args.apply:
        return apply(args.db, engine, actor=args.actor)
    if args.revoke_baseline:
        return revoke_baseline(args.db, engine, args.revoke_baseline, reason=args.reason or "", actor=args.actor)
    if args.online:
        from migration import online

        return online.run(args.db, engine, args.online, drop=args.drop)
    from migration import baseline

    return baseline.adopt(args.db, engine, reference_url=args.reference_url, actor=args.actor)


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = _dispatch(args)
    except MigrationStateError as exc:
        print(json.dumps({"runner": RUNNER_VERSION, "db_role": args.db, "state": "refused", "reason": str(exc),
                          "written": False}, ensure_ascii=False, indent=1))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=1, default=str))
    return 2 if receipt.get("state") == "refused" else 0


if __name__ == "__main__":
    sys.exit(main())
