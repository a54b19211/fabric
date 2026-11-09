"""独立的索引 worker（v3 3.6：执行不挂在发表循环上）。必须使用独立 History 连接。

    python -m migration.index_publication worker -c 4     # 跑 worker（4 个线程），Ctrl-C 停

索引表由 History 角色安装：python -m migration.history（= python -m migration.runner --db history --apply）。
"""

import argparse
import json
import signal
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", help="运行独立的索引 worker")
    worker.add_argument("-c", "--concurrency", type=int, default=2)
    worker.add_argument("--scan-every", type=float, default=30.0, help="回填扫描间隔（秒）")
    args = parser.parse_args()
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
