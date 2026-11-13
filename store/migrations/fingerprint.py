"""Structure fingerprints: what a schema contains, as comparable facts.

A fact is (key, definition): ("column", "documents.title") -> "text|text|YES|~|~", ("function", "f(a integer)") ->
a digest of its body, and so on. Only catalog reads (pg_catalog / information_schema) of the connection's current
schema; no data table is read; objects that belong to an extension (vector, pg_search ...) are not counted. A role's
fingerprint of a prefix of its files is the set of facts for the keys that the role's files own (the keys whose fact
differs between the empty base and the fully installed library).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from store.migrations.manifests import VERSIONS_DIR, Manifest

Facts = dict[tuple, str]
Fingerprint = frozenset  # of (key, definition)

_EXTENSION_OWNED = (
    "EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{0}'::regclass AND d.objid = {1}.oid AND d.deptype = 'e')"
)
_QUERIES = {
    "table": "SELECT c.relname, '' FROM pg_class c WHERE c.relnamespace = current_schema()::regnamespace "
             "AND c.relkind IN ('r', 'p') AND NOT " + _EXTENSION_OWNED.format("pg_class", "c"),
    "view": "SELECT v.viewname, v.definition FROM pg_views v JOIN pg_class c ON c.oid = "
            "(quote_ident(v.schemaname) || '.' || quote_ident(v.viewname))::regclass "
            "WHERE v.schemaname = current_schema() AND NOT " + _EXTENSION_OWNED.format("pg_class", "c"),
    "column": "SELECT c.relname || '.' || a.attname, format_type(a.atttypid, a.atttypmod) || '|' || a.attnotnull::text "
              "|| '|' || coalesce(pg_get_expr(d.adbin, d.adrelid), '~') FROM pg_attribute a "
              "JOIN pg_class c ON c.oid = a.attrelid LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid "
              "AND d.adnum = a.attnum WHERE c.relnamespace = current_schema()::regnamespace "
              "AND c.relkind IN ('r', 'p', 'v', 'm') AND a.attnum > 0 AND NOT a.attisdropped AND NOT "
              + _EXTENSION_OWNED.format("pg_class", "c"),
    "constraint": "SELECT r.relname || '.' || c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
                  "JOIN pg_class r ON r.oid = c.conrelid WHERE c.connamespace = current_schema()::regnamespace "
                  "AND NOT " + _EXTENSION_OWNED.format("pg_class", "r"),
    "index": "SELECT i.tablename || '.' || i.indexname, i.indexdef FROM pg_indexes i JOIN pg_class r ON r.oid = "
             "(quote_ident(i.schemaname) || '.' || quote_ident(i.tablename))::regclass "
             "WHERE i.schemaname = current_schema() AND NOT " + _EXTENSION_OWNED.format("pg_class", "r"),
    "trigger": "SELECT c.relname || '.' || t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
               "JOIN pg_class c ON c.oid = t.tgrelid WHERE c.relnamespace = current_schema()::regnamespace "
               "AND NOT t.tgisinternal AND NOT " + _EXTENSION_OWNED.format("pg_class", "c"),
    "function": "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', "
                "md5(p.prosrc || pg_get_function_result(p.oid)) FROM pg_proc p "
                "WHERE p.pronamespace = current_schema()::regnamespace AND NOT "
                + _EXTENSION_OWNED.format("pg_proc", "p"),
    "sequence": "SELECT sequence_name, data_type FROM information_schema.sequences "
                "WHERE sequence_schema = current_schema()",
    "rule": "SELECT r.tablename || '.' || r.rulename, r.definition FROM pg_rules r JOIN pg_class c ON c.oid = "
            "(quote_ident(r.schemaname) || '.' || quote_ident(r.tablename))::regclass "
            "WHERE r.schemaname = current_schema() AND NOT " + _EXTENSION_OWNED.format("pg_class", "c"),
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


# facts that belong to a table: key[1] is "table.name"
_OF_TABLE = frozenset({"column", "constraint", "index", "trigger", "rule"})


def project(facts: Facts, keys) -> Fingerprint:
    """The facts the role is answerable for: its own keys, and EVERY column, constraint, index, trigger and rule of a table
    the role creates - so a column or index nobody declared on such a table shows up as a difference instead of being
    ignored. The tables of other roles (documents, entity tables ...) are compared only on the keys this role adds to them;
    what else sits on them is theirs. Functions and sequences are compared by the names the role declares."""
    tables = {key[1] for key in keys if key[0] == "table"}
    return frozenset((key, definition) for key, definition in facts.items()
                     if key in keys or (key[0] in _OF_TABLE and key[1].split(".", 1)[0] in tables))


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
    # a fact that only an intermediate state held (a function replaced later) belongs to the role too
    owned = frozenset(key for facts in steps for key, definition in facts.items() if base.get(key) != definition)
    return [project(base, owned), *[project(facts, owned) for facts in steps]], owned


def difference(observed: Fingerprint, expected: Fingerprint) -> dict:
    """Missing, extra and different facts of `observed` against `expected`, by key and kind."""
    seen, want = dict(observed), dict(expected)
    return {
        "missing": sorted(f"{k[0]} {k[1]}" for k in want if k not in seen),
        "extra": sorted(f"{k[0]} {k[1]}" for k in seen if k not in want),
        "different": sorted(f"{k[0]} {k[1]}" for k in want if k in seen and want[k] != seen[k]),
    }
