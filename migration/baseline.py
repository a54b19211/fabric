"""Taking over a database that was migrated before there was a ledger (--adopt-baseline).

What it establishes is narrow: the structure observed today equals a fresh install of the first k files of the role's
manifest. The ledger rows it writes are `observed_baseline` events (or `compat_mapped` for a named, reviewed
mapping); they say "observed equal on this date", never "this file was executed in the past". Rows written by data
steps cannot be seen in a structure, so a file that changes rows is only taken over if it declares a postcondition and
the postcondition holds now.

The fingerprints of the clean prefixes come from running the files in a throwaway empty library (a container this
module starts and removes by the IDs it recorded, or a library given with --reference-url). Nothing is written to the
target unless a prefix is established; an unknown structure produces a difference report and stops.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, text

from migration import compat_mappings, fingerprint, ledger, runner
from migration.app_ledger import MigrationStateError
from migration.manifests import MANIFESTS, POSTCONDITIONS, TRANSACTION_CONTROL_ALLOWED, Manifest
from migration.sqlcheck import inspect_sql


@dataclass
class Refusal(Exception):
    reason: str
    detail: dict

    def receipt(self, role: str) -> dict:
        return {"runner": runner.RUNNER_VERSION, "db_role": role, "action": "adopt-baseline", "state": "refused",
                "reason": self.reason, "written": False, **self.detail}


def _postconditions_hold(conn, name: str) -> tuple[bool, str]:
    for sql in POSTCONDITIONS[name]:
        try:
            with conn.begin_nested():
                if conn.exec_driver_sql(sql).scalar_one() is not True:
                    return False, sql
        except Exception as exc:  # noqa: BLE001 - a missing table or column is a failed check, said as such
            return False, f"{sql} -> {type(exc).__name__}"
    return True, ""


def _undecidable(name: str) -> Refusal:
    return Refusal("data_file_without_postcondition",
                   {"file": name, "detail": f"{name} changes rows and declares no postcondition, or leaves no "
                    "structural trace and declares none: whether it ran cannot be told, so nothing was adopted"})


def settle_prefix(manifest: Manifest, candidates: list[int], conn) -> int:
    """Among prefix lengths with the observed structure, the longest one every row-changing file of which is shown to
    have run. Files that leave no structural trace (between the shortest and longest candidate) are decided only by their
    postconditions, one by one; a candidate is never picked just for being the longest."""
    lowest, highest = min(candidates), max(candidates)
    k = highest
    for number in range(lowest + 1, highest + 1):
        name = manifest.files[number - 1]
        if not POSTCONDITIONS.get(name):
            raise _undecidable(name)
        held, _ = _postconditions_hold(conn, name)
        if not held:
            k = number - 1
            break
    for number, name in enumerate(manifest.files[:k], start=1):
        report = inspect_sql((runner.VERSIONS_DIR / name).read_text(encoding="utf-8"),
                             allow_transaction_control=name in TRANSACTION_CONTROL_ALLOWED)
        if not report.data_statements:
            continue
        if not POSTCONDITIONS.get(name):
            raise _undecidable(name)
        held, failed = _postconditions_hold(conn, name)
        if not held:
            raise Refusal("postcondition_failed",
                          {"file": name, "detail": f"the structure is that of a fresh install up to {name}, but its "
                           f"data step is not done: {failed}; run the data step with a corrective migration"})
    return k


def _nearest(fingerprints, target) -> tuple[int, dict]:
    sizes = [len(fp ^ target) for fp in fingerprints]
    best = sizes.index(min(sizes))
    return best, fingerprint.difference(target, fingerprints[best])


def decide(manifest: Manifest, fingerprints, target, conn) -> tuple[int, str, dict]:
    """(prefix length, event, evidence) or Refusal."""
    sha = fingerprint.digest(target)
    candidates = [k for k, fp in enumerate(fingerprints) if fp == target]
    if candidates:
        if min(candidates) == 0:
            raise Refusal("nothing_to_adopt", {"detail": "the database has none of the role's objects: use --apply"})
        return settle_prefix(manifest, candidates, conn), "observed_baseline", {
            "fingerprint_sha256": sha, "candidate_prefixes": candidates}
    mapping = compat_mappings.find(manifest.role, sha)
    if mapping is not None and mapping.treatment == "equivalent_to_prefix":
        k = settle_prefix(manifest, [int(mapping.target)], conn)
        return k, "compat_mapped", {"fingerprint_sha256": sha, "mapping": mapping.name,
                                    "meaning": mapping.meaning, "reviewed_by": mapping.reviewed_by}
    nearest, diff = _nearest(fingerprints, target)
    detail = {"nearest_prefix": nearest, "diff": diff, "diff_sha256": sha}
    if mapping is not None:
        detail.update(mapping=mapping.name, fix_migration=mapping.target)
        raise Refusal("requires_fix_migration", detail)
    raise Refusal("fingerprint_matches_no_prefix", detail)


def _write(conn, role: str, manifest: Manifest, k: int, event: str, evidence: dict, actor: str) -> None:
    observed_at = conn.execute(text("SELECT clock_timestamp()")).scalar_one().isoformat()
    ledger.ensure(conn)
    for number, name in enumerate(manifest.files[:k], start=1):
        ledger.append(conn, role, name, event, (runner.VERSIONS_DIR / name).read_text(encoding="utf-8"), actor=actor,
                      evidence={**evidence, "prefix_length": k, "position": number, "observed_at": observed_at,
                                "runner": runner.RUNNER_VERSION})


def _reference(role: str, reference_engine, reference_url):
    if reference_engine is not None:
        return reference_engine, None
    if reference_url:
        return create_engine(reference_url), None
    from migration.scratch_db import ScratchPostgres

    scratch = ScratchPostgres()
    return create_engine(scratch.start()), scratch


def adopt(role: str, engine, *, reference_url: str | None = None, reference_engine=None,
          actor: str | None = None) -> dict:
    if role not in ("history", "publication"):
        raise MigrationStateError("only the history and publication roles are taken over; the object library "
                                  "already has its ledger")
    actor = actor or runner.default_actor()
    manifest = MANIFESTS[role]
    with runner.migration_lock(engine) as conn:
        state = ledger.read_current(conn, role)
        conn.rollback()
        if state.present:
            return Refusal("ledger_not_empty", {"detail": "--adopt-baseline runs once, on an empty ledger"}
                           ).receipt(role)
        ref, scratch = _reference(role, reference_engine, reference_url)
        try:
            return _adopt_locked(role, manifest, conn, ref, actor)
        except Refusal as refusal:
            conn.rollback()
            return refusal.receipt(role)
        finally:
            if scratch is not None:
                scratch.stop()


def _adopt_locked(role: str, manifest: Manifest, conn, ref, actor: str) -> dict:
    with ref.connect() as reference_conn:
        if fingerprint.read_facts(reference_conn):
            raise Refusal("reference_not_empty", {"detail": "the reference library must be empty"})
    fingerprints, owned = fingerprint.prefix_fingerprints(manifest, ref)
    target = fingerprint.project(fingerprint.read_facts(conn), owned)
    k, event, evidence = decide(manifest, fingerprints, target, conn)
    _write(conn, role, manifest, k, event, evidence, actor)
    runner.commit(conn)
    return {"runner": runner.RUNNER_VERSION, "db_role": role, "action": "adopt-baseline", "state": "adopted",
            "event": event, "prefix_length": k, "adopted": list(manifest.files[:k]),
            "pending": list(manifest.files[k:]), "evidence": evidence, "written": True,
            "meaning": runner.OBSERVED_MEANING}
