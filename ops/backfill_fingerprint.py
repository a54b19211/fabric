"""给加指纹列（迁移 V042）以前存的版本补上内容指纹（store.history.fingerprint）。

    python ops/backfill_fingerprint.py [--batch 500]

只补 fingerprint 为空的行，一批一个事务，可以随时停、再跑接着补。不补也不会出错：存版本时没指纹的当前版本会当场算。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from store import history  # noqa: E402


def backfill(batch: int) -> int:
    done = 0
    while True:
        with history.history_engine().begin() as conn:
            rows = conn.execute(text(
                "SELECT version_id, source_id, payload->>'raw_content' AS raw FROM history_versions "
                "WHERE fingerprint IS NULL LIMIT :n FOR UPDATE SKIP LOCKED"), {"n": batch}).mappings().all()
            for row in rows:
                conn.execute(text("UPDATE history_versions SET fingerprint=:fp WHERE version_id=:id"),
                             {"fp": history.fingerprint(row["source_id"], row["raw"]), "id": row["version_id"]})
        done += len(rows)
        if len(rows) < batch:
            return done


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", type=int, default=500)
    print(f"filled {backfill(parser.parse_args().batch)} fingerprints")
