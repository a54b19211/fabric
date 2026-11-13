"""The append-only ledger of the History and publication roles (and the sha256 record of the object library).

The object library keeps its own schema_migrations rows untouched (store.migrations.app_ledger). Every other record goes
into one event table that is never updated or deleted: a correction is a later event for the same file, and the view
`fabric_schema_ledger_current` shows the latest event per (db_role, version). Nothing in this module deletes,
rewrites or truncates a row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy import text

EVENTS = "fabric_schema_ledger_events"
CURRENT = "fabric_schema_ledger_current"
REFERENCE_LOG = "fabric_reference_data_log"
PRESENT_EVENTS = ("applied", "observed_baseline", "compat_mapped")
ALL_EVENTS = (*PRESENT_EVENTS, "baseline_revoked")

DDL = (
    f"""CREATE TABLE IF NOT EXISTS {EVENTS} (
        event_id bigserial PRIMARY KEY,
        db_role text NOT NULL,
        version text NOT NULL,
        file text NOT NULL,
        event text NOT NULL CHECK (event IN ('applied','observed_baseline','baseline_revoked','compat_mapped')),
        checksum_md5 text NOT NULL,
        sha256 text NOT NULL,
        evidence jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        actor text NOT NULL,
        at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""",
    f"""CREATE TABLE IF NOT EXISTS {REFERENCE_LOG} (
        log_id bigserial PRIMARY KEY,
        db_role text NOT NULL,
        step text NOT NULL,
        result jsonb NOT NULL,
        actor text NOT NULL,
        at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""",
    """CREATE OR REPLACE FUNCTION fabric_ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION USING MESSAGE = 'the migration ledger is append-only: ' || TG_OP || ' on ' || TG_TABLE_NAME;
    END $$""",
    f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'fabric_schema_ledger_events_append_only'
                       AND tgrelid = to_regclass('{EVENTS}')) THEN
            CREATE TRIGGER fabric_schema_ledger_events_append_only BEFORE UPDATE OR DELETE ON {EVENTS}
                FOR EACH ROW EXECUTE FUNCTION fabric_ledger_append_only();
            CREATE TRIGGER fabric_schema_ledger_events_no_truncate BEFORE TRUNCATE ON {EVENTS}
                FOR EACH STATEMENT EXECUTE FUNCTION fabric_ledger_append_only();
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'fabric_reference_data_log_append_only'
                       AND tgrelid = to_regclass('{REFERENCE_LOG}')) THEN
            CREATE TRIGGER fabric_reference_data_log_append_only BEFORE UPDATE OR DELETE ON {REFERENCE_LOG}
                FOR EACH ROW EXECUTE FUNCTION fabric_ledger_append_only();
        END IF;
    END $$""",
    f"""CREATE OR REPLACE VIEW {CURRENT} AS
        SELECT DISTINCT ON (db_role, version) event_id, db_role, version, file, event, checksum_md5, sha256,
               evidence, actor, at
        FROM {EVENTS} ORDER BY db_role, version, event_id DESC""",
)


def md5(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def known_checksums(file: str) -> set[str]:
    """Every checksum a commit of this repository had for this file name (today's content and earlier ones)."""
    from store.migrations.known_migrations import KNOWN

    return {checksum for _v, _d, checksum, kind, target, _s in KNOWN if target == file and kind in ("current", "earlier")}


def table_exists(conn, name: str) -> bool:
    return conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar_one()


def ensure(conn) -> None:
    for statement in DDL:
        conn.exec_driver_sql(statement)


@dataclass
class Entry:
    file: str
    event: str
    checksum_md5: str
    sha256: str
    evidence: dict
    at: object = None


@dataclass
class LedgerState:
    present: dict[str, Entry] = field(default_factory=dict)  # file -> latest event, for files that are in
    revoked: dict[str, Entry] = field(default_factory=dict)  # file -> latest event, for files whose baseline was revoked
    table: bool = False


def read_current(conn, role: str) -> LedgerState:
    state = LedgerState(table=table_exists(conn, CURRENT) or table_exists(conn, EVENTS))
    if not state.table or not table_exists(conn, CURRENT):
        return state
    rows = conn.execute(text(
        f"SELECT file, event, checksum_md5, sha256, evidence, at FROM {CURRENT} WHERE db_role=:r ORDER BY version"),
        {"r": role}).mappings()
    for row in rows:
        entry = Entry(row["file"], row["event"], row["checksum_md5"], row["sha256"], row["evidence"], row["at"])
        (state.present if row["event"] in PRESENT_EVENTS else state.revoked)[entry.file] = entry
    return state


def append(conn, role: str, file: str, event: str, content: str, *, evidence: dict | None = None,
           actor: str) -> None:
    version = file.split("__", 1)[0]
    conn.execute(text(
        f"INSERT INTO {EVENTS}(db_role, version, file, event, checksum_md5, sha256, evidence, actor) "
        "VALUES (:role, :version, :file, :event, :md5, :sha, CAST(:evidence AS jsonb), :actor)"),
        {"role": role, "version": version, "file": file, "event": event, "md5": md5(content), "sha": sha256(content),
         "evidence": json.dumps(evidence or {}, sort_keys=True), "actor": actor})


def append_revocation(conn, role: str, file: str, content: str, *, reason: str, actor: str) -> None:
    append(conn, role, file, "baseline_revoked", content, evidence={"reason": reason}, actor=actor)
