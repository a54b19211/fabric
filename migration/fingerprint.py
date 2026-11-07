"""Structure fingerprints: what a schema contains, as comparable facts.

A fact is (key, definition): ("column", "documents", "title") -> "text|text|YES||", ("function", "f(a integer)") ->
sha256 of its body, and so on. Only catalog reads (pg_catalog / information_schema) of the connection's current
schema; no data table is read. A role's fingerprint of a prefix of its files is the set of facts for the keys that
the role's files own (the keys whose fact differs between the empty base and the fully installed library).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from migration.manifests import VERSIONS_DIR, Manifest

Facts = dict[tuple, str]
Fingerprint = frozenset  # of (key, definition)

_QUERIES = {
    "table": "SELECT table_name, '' FROM information_schema.tables "
             "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'",
    "view": "SELECT viewname, definition FROM pg_views WHERE schemaname = current_schema()",
    "column": "SELECT table_name || '.' || column_name, concat_ws('|', data_type, udt_name, is_nullable, "
              "column_default, character_maximum_length) "
              "FROM information_schema.columns WHERE table_schema = current_schema()",
    "constraint": "SELECT c.conrelid::regclass::text || '.' || c.conname, pg_get_constraintdef(c.oid) "
                  "FROM pg_constraint c WHERE c.connamespace = current_schema()::regnamespace",
    "index": "SELECT tablename || '.' || indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()",
    "trigger": "SELECT c.relname || '.' || t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
               "JOIN pg_class c ON c.oid = t.tgrelid WHERE c.relnamespace = current_schema()::regnamespace "
               "AND NOT t.tgisinternal",
    "function": "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', "
                "md5(p.prosrc || pg_get_function_result(p.oid)) FROM pg_proc p "
                "WHERE p.pronamespace = current_schema()::regnamespace",
    "sequence": "SELECT sequence_name, data_type FROM information_schema.sequences "
                "WHERE sequence_schema = current_schema()",
    "rule": "SELECT tablename || '.' || rulename, definition FROM pg_rules WHERE schemaname = current_schema()",
}


def read_facts(conn) -> Facts:
    schema = conn.execute(text("SELECT current_schema()")).scalar_one()
    facts: Facts = {}
    for kind, sql in _QUERIES.items():
        for name, definition in conn.exec_driver_sql(sql):
            facts[(kind, name.replace(schema + ".", ""))] = (definition or "").replace(schema + ".", "")
    return facts


def digest(fingerprint) -> str:
    rows = sorted([list(key), definition] for key, definition in fingerprint)
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def project(facts: Facts, keys) -> Fingerprint:
    return frozenset((key, facts[key]) for key in keys if key in facts)


def run_files(conn, names) -> None:
    for name in names:
        conn.exec_driver_sql((VERSIONS_DIR / name).read_text(encoding="utf-8"))


def prefix_fingerprints(manifest: Manifest, engine) -> tuple[list[Fingerprint], frozenset]:
    """The role's fingerprint after 0, 1, ... N of its files, built by running them in order on `engine`, which must
    be an empty library (a throwaway one). Returns the fingerprints and the keys the role owns."""
    with engine.begin() as conn:
        run_files(conn, manifest.base_files)
        base = read_facts(conn)
    steps = []
    for name in manifest.files:
        with engine.begin() as conn:
            run_files(conn, [name])
            steps.append(read_facts(conn))
    last = steps[-1] if steps else base
    owned = frozenset(key for key, definition in last.items() if base.get(key) != definition)
    # a fact that only an intermediate state held (a function replaced later) belongs to the role too
    for facts in steps:
        owned |= frozenset(key for key, definition in facts.items() if base.get(key) != definition)
    return [project(base, owned), *[project(facts, owned) for facts in steps]], owned


def difference(observed: Fingerprint, expected: Fingerprint) -> dict:
    """Missing, extra and different facts of `observed` against `expected`, by key and kind."""
    seen, want = dict(observed), dict(expected)
    return {
        "missing": sorted(f"{k[0]} {k[1]}" for k in want if k not in seen),
        "extra": sorted(f"{k[0]} {k[1]}" for k in seen if k not in want),
        "different": sorted(f"{k[0]} {k[1]}" for k in want if k in seen and want[k] != seen[k]),
    }
