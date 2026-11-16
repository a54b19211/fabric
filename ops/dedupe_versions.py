"""Delete consecutive duplicate versions: a version whose fingerprint equals the one of the version it replaced.

    python -m ops.dedupe_versions --plan              # counts per source, references, pinned versions
    python -m ops.dedupe_versions --apply [--batch 200]

The earlier version of each run of identical versions is kept; the later ones are deleted and the kept version's
replaced_by takes the last deleted one's replaced_by. Per removed version, in the same transaction:
  - derived rows (clean text, identifiers, profiles, claims, checks, stage waiting, version rank, registry diffs
    *to* it) are deleted: they are the kept version's again;
  - references that mean "this content" are re-pointed to the kept version: registry diffs/changes *from* it,
    attachment links, legacy_aliases.real_version_id / equivalent_version_id;
  - index publications that were never sent, or are already withdrawn (disabled), are deleted.
Kept, not deleted ("pinned"): versions with an index publication that is live in WeKnora (sent, not disabled; the
index worker withdraws those through its normal path, a later run removes them) and versions that are a legacy id
(legacy_aliases.legacy_version_id: old citations resolve by that id).
Removed -> kept goes into dedupe_version_map; the last step re-points fabric_runs.version_ids from it.
The no-delete rules and the append-only trigger are disabled inside each transaction only. Idempotent: every
batch recomputes from the current state. Stop scheduler, fetch worker, clean and index before --apply.
"""
import argparse
from collections import defaultdict

from sqlalchemy import text

from store.records import fabric_engine

DUPES = ("SELECT n.version_id AS rm, n.source_id, n.source_record_id FROM fabric_versions n "
         "JOIN fabric_versions o ON o.replaced_by=n.version_id AND o.fingerprint=n.fingerprint")
SENT = "(p.first_send_at IS NOT NULL OR p.knowledge_id IS NOT NULL OR p.receipt<>'{}')"
PINNED = (f"SELECT version_id FROM index_publications p WHERE {SENT} AND p.state<>'disabled' "
          "UNION SELECT legacy_version_id FROM legacy_aliases")
DELETE_FROM = ("derived_clean_text", "derived_identifiers", "derived_profiles", "derived_text_claims",
               "derived_extraction_checks", "derived_extraction_check_tasks", "derived_stage_waiting",
               "derived_quality_scores", "derived_authority_resolutions", "fabric_version_rank")
REPOINT = (("derived_registry_diff_runs", "from_version_id"), ("derived_registry_changes", "from_version_id"),
           ("fabric_attachment_links", "parent_version_id"), ("fabric_attachment_links", "attachment_version_id"),
           ("legacy_aliases", "real_version_id"), ("legacy_aliases", "equivalent_version_id"))


def plan():
    with fabric_engine().begin() as conn:
        conn.execute(text(f"CREATE TEMP TABLE d AS {DUPES}"))
        conn.execute(text(f"CREATE TEMP TABLE pin AS {PINNED}"))
        rows = conn.execute(text(
            "SELECT source_id, count(*) AS dupes, count(DISTINCT source_record_id) AS records, "
            "count(*) FILTER (WHERE rm IN (SELECT version_id FROM pin)) AS pinned FROM d GROUP BY 1 ORDER BY 2 DESC"))
        for r in rows.mappings():
            print(f"{r['source_id']:<18} duplicates {r['dupes']:>7}  records {r['records']:>5}  pinned {r['pinned']:>5}")
        forks = conn.execute(text("SELECT count(*) FROM (SELECT replaced_by FROM fabric_versions "
                                  "WHERE replaced_by IN (SELECT rm FROM d) GROUP BY 1 HAVING count(*)>1) f")).scalar()
        print("forked chains (one version replaced twice):", forks)
        for table in DELETE_FROM + ("index_publications",):
            n = conn.execute(text(f"SELECT count(*) FROM {table} WHERE version_id IN (SELECT rm FROM d)")).scalar()
            print(f"  delete {table}: {n}")
        n = conn.execute(text("SELECT count(*) FROM derived_registry_diff_runs WHERE to_version_id IN (SELECT rm FROM d)"
                              )).scalar()
        print(f"  delete derived_registry_diff_runs (to): {n}")
        for table, column in REPOINT:
            n = conn.execute(text(f"SELECT count(*) FROM {table} WHERE {column} IN (SELECT rm FROM d)")).scalar()
            print(f"  re-point {table}.{column}: {n}")


def _segments(conn, keys):
    """removed -> kept, and kept -> its new replaced_by, for these (source, record) pairs."""
    rows = conn.execute(text(
        "SELECT v.version_id, v.fingerprint, v.replaced_by, EXISTS (SELECT 1 FROM dedupe_pinned p WHERE p.version_id=v.version_id) AS pinned "
        "FROM fabric_versions v JOIN unnest(CAST(:s AS text[]), CAST(:r AS text[])) k(s, r) "
        "ON v.source_id=k.s AND v.source_record_id=k.r"), {"s": [k[0] for k in keys], "r": [k[1] for k in keys]})
    rows = {r["version_id"]: r for r in rows.mappings()}
    previous = {r["replaced_by"]: v for v, r in rows.items() if r["replaced_by"]}
    mapping, successor = {}, {}
    for vid, row in rows.items():
        before = rows.get(previous.get(vid))
        if before is not None and before["fingerprint"] == row["fingerprint"] and not row["pinned"]:
            continue  # inside a run: reached from its head
        last, nxt = vid, row["replaced_by"]
        while nxt in rows and rows[nxt]["fingerprint"] == row["fingerprint"] and not rows[nxt]["pinned"]:
            mapping[nxt], last, nxt = vid, nxt, rows[nxt]["replaced_by"]
        if last != vid:
            successor[vid] = rows[last]["replaced_by"]
    return mapping, successor


def _switch(conn, enable):
    verb = "ENABLE" if enable else "DISABLE"
    rules = conn.execute(text("SELECT tablename, rulename FROM pg_rules WHERE schemaname='public' "
                              "AND (rulename LIKE '%no_delete' OR rulename LIKE '%no_update')")).all()
    for table, rule in rules:
        conn.execute(text(f"ALTER TABLE {table} {verb} RULE {rule}"))
    conn.execute(text(f"ALTER TABLE fabric_versions {verb} TRIGGER fabric_versions_append_only"))


def _apply_batch(conn, mapping, successor):
    conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS m (rm char(64) PRIMARY KEY, kept char(64)) ON COMMIT DROP"))
    conn.execute(text("INSERT INTO m SELECT * FROM unnest(CAST(:rm AS text[]), CAST(:kept AS text[]))"),
                 {"rm": list(mapping), "kept": list(mapping.values())})
    conn.execute(text("INSERT INTO dedupe_version_map SELECT m.rm, m.kept, v.source_id FROM m "
                      "JOIN fabric_versions v ON v.version_id=m.rm ON CONFLICT DO NOTHING"))
    _switch(conn, False)
    for table in DELETE_FROM:
        conn.execute(text(f"DELETE FROM {table} WHERE version_id IN (SELECT rm FROM m)"))
    conn.execute(text("DELETE FROM derived_registry_changes WHERE to_version_id IN (SELECT rm FROM m)"))
    conn.execute(text("DELETE FROM derived_registry_diff_runs WHERE to_version_id IN (SELECT rm FROM m)"))
    conn.execute(text("DELETE FROM index_publications WHERE version_id IN (SELECT rm FROM m)"))  # none live: pinned
    for table, column in REPOINT:
        conn.execute(text(f"UPDATE {table} t SET {column}=m.kept FROM m WHERE t.{column}=m.rm"))
    conn.execute(text("UPDATE fabric_versions SET replaced_by=NULL WHERE version_id=ANY(:k)"), {"k": list(successor)})
    conn.execute(text("DELETE FROM fabric_versions WHERE version_id IN (SELECT rm FROM m)"))
    conn.execute(text("UPDATE fabric_versions v SET replaced_by=s.nxt FROM unnest(CAST(:k AS text[]), "
                      "CAST(:n AS text[])) s(kept, nxt) WHERE v.version_id=s.kept AND s.nxt IS NOT NULL"),
                 {"k": list(successor), "n": list(successor.values())})
    _switch(conn, True)
    conn.execute(text("DROP TABLE m"))


def apply(batch):
    with fabric_engine().begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS dedupe_version_map "
                          "(removed char(64) PRIMARY KEY, kept char(64) NOT NULL, source_id text NOT NULL)"))
        # pinned versions as an indexed table, computed once: an IN/NOT IN over the subquery is evaluated per row
        conn.execute(text("DROP TABLE IF EXISTS dedupe_pinned"))
        conn.execute(text(f"CREATE TABLE dedupe_pinned AS SELECT DISTINCT version_id FROM ({PINNED}) x "
                          "WHERE version_id IS NOT NULL"))
        conn.execute(text("ALTER TABLE dedupe_pinned ADD PRIMARY KEY (version_id)"))
    total = defaultdict(int)
    while True:
        with fabric_engine().begin() as conn:
            keys = conn.execute(text(f"SELECT DISTINCT d.source_id, d.source_record_id FROM ({DUPES}) d "
                                     f"WHERE NOT EXISTS (SELECT 1 FROM dedupe_pinned pin WHERE pin.version_id = d.rm) "
                                     f"LIMIT {int(batch)}")).all()
            if not keys:
                break
            mapping, successor = _segments(conn, keys)
            if not mapping:
                break
            _apply_batch(conn, mapping, successor)
            total["versions"] += len(mapping)
        print("deleted", total["versions"], flush=True)
    _repoint_runs()


def _repoint_runs(chunk=5000):
    """fabric_runs.version_ids: removed ids -> kept ids (resolving maps of maps), one chunk of runs per transaction."""
    with fabric_engine().begin() as conn:
        while conn.execute(text("UPDATE dedupe_version_map a SET kept=b.kept FROM dedupe_version_map b "
                                "WHERE a.kept=b.removed")).rowcount:
            pass
    after = "00000000-0000-0000-0000-000000000000"
    while True:
        with fabric_engine().begin() as conn:
            ids = [r[0] for r in conn.execute(text("SELECT id FROM fabric_runs WHERE id>CAST(:a AS uuid) "
                                                   "ORDER BY id LIMIT :n"), {"a": after, "n": chunk})]
            if not ids:
                return
            conn.execute(text("""
                UPDATE fabric_runs r SET version_ids=(
                  SELECT jsonb_agg(COALESCE(m.kept, e.v) ORDER BY e.o)
                  FROM jsonb_array_elements_text(r.version_ids) WITH ORDINALITY e(v, o)
                  LEFT JOIN dedupe_version_map m ON m.removed=e.v)
                WHERE r.id=ANY(CAST(:ids AS uuid[])) AND EXISTS (
                  SELECT 1 FROM jsonb_array_elements_text(r.version_ids) e(v)
                  JOIN dedupe_version_map m ON m.removed=e.v)"""), {"ids": [str(i) for i in ids]})
            after = str(ids[-1])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--batch", type=int, default=200, help="records per transaction")
    args = parser.parse_args()
    plan() if args.plan else apply(args.batch)
