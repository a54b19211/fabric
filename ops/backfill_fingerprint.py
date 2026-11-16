"""给加指纹列（迁移 V042）以前存的版本补上内容指纹（store.records.fingerprint）。

    python ops/backfill_fingerprint.py [--batch 500]
    python ops/backfill_fingerprint.py --recompute who_don [--dry-run]

只补 fingerprint 为空的行，一批一个事务，可以随时停、再跑接着补。不补也不会出错：存版本时没指纹的当前版本会当场算。

--recompute <来源>：指纹规则（store.records.normalized_raw）改了以后，把这个来源已存版本的指纹按现在的规则重算
（一个事务，适合小来源）。指纹平时只许填一次，这里在事务里临时停掉只追加触发器；先停 scheduler、fetch-worker、
clean、index。重算后原来来回变的版本指纹相同，用 ops/dedupe_versions.py 删重复。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from store import records  # noqa: E402


def backfill(batch: int) -> int:
    done = 0
    while True:
        with records.fabric_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT version_id, source_id, payload->>'raw_content' AS raw FROM fabric_versions "
                "WHERE fingerprint IS NULL LIMIT :n FOR UPDATE SKIP LOCKED"), {"n": batch}).mappings().all()
            for row in rows:
                conn.execute(text("UPDATE fabric_versions SET fingerprint=:fp WHERE version_id=:id"),
                             {"fp": records.fingerprint(row["source_id"], row["raw"]), "id": row["version_id"]})
        done += len(rows)
        if len(rows) < batch:
            return done


def recompute(source: str, dry_run: bool) -> tuple[int, int]:
    """(versions of the source, versions whose fingerprint differs under the current rule and was rewritten)."""
    with records.fabric_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT version_id, fingerprint, payload->>'raw_content' AS raw FROM fabric_versions "
            "WHERE source_id=:s FOR UPDATE"), {"s": source}).mappings().all()
        changed = [{"id": r["version_id"], "fp": fp} for r in rows
                   if (fp := records.fingerprint(source, r["raw"])) != r["fingerprint"]]
        if changed and not dry_run:
            conn.execute(text("ALTER TABLE fabric_versions DISABLE TRIGGER fabric_versions_append_only"))
            conn.execute(text("UPDATE fabric_versions SET fingerprint=:fp WHERE version_id=:id"), changed)
            conn.execute(text("ALTER TABLE fabric_versions ENABLE TRIGGER fabric_versions_append_only"))
    return len(rows), len(changed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--recompute", metavar="SOURCE")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.recompute:
        total, changed = recompute(args.recompute, args.dry_run)
        print(f"{args.recompute}: {total} versions, {changed} fingerprints "
              + ("would change" if args.dry_run else "recomputed"))
    else:
        print(f"filled {backfill(args.batch)} fingerprints")
