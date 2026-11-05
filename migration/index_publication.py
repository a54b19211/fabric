"""索引表的显式迁移，和独立的索引 worker（v3 3.6：执行不挂在发表循环上）。必须使用独立 History 连接。

    python -m migration.index_publication                 # 迁移
    python -m migration.index_publication worker -c 4     # 跑 worker（4 个线程），Ctrl-C 停
"""

import argparse
import json
import signal
import threading
from pathlib import Path

from sqlalchemy import text

from db.history import history_engine

VERSIONS = ("V020__weknora_index.sql", "V024__index_artifact.sql", "V027__index_publications_recent.sql",
            "V028__index_worker.sql", "V031__index_scheduling.sql")


def migrate_index():
    sql = "\n".join((Path(__file__).parent / "versions" / name).read_text() for name in VERSIONS)
    with history_engine().begin() as conn:
        # 现役有读写时不无限等锁：等不到就整笔回滚，稍后再跑
        conn.exec_driver_sql("SET LOCAL lock_timeout='3s'")
        conn.exec_driver_sql("SET LOCAL statement_timeout='60s'")
        for statement in sql.split(";"):
            statement = "\n".join(s for s in statement.splitlines() if not s.lstrip().startswith("--")).strip()
            if statement:
                conn.execute(text(statement))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    worker = sub.add_parser("worker", help="运行独立的索引 worker")
    worker.add_argument("-c", "--concurrency", type=int, default=2)
    worker.add_argument("--scan-every", type=float, default=30.0, help="回填扫描间隔（秒）")
    args = parser.parse_args()
    if args.command != "worker":
        migrate_index()
        return
    from db import index_publication

    if not index_publication.configured():
        print(json.dumps({"index": {"state": "disabled"}}), flush=True)
        return
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    counts = index_publication.run_worker(concurrency=args.concurrency, scan_every_seconds=args.scan_every, stop=stop)
    print(json.dumps({"index": {"state": "stopped", "processed": counts}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
