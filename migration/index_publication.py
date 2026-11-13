"""索引 worker 的启动入口：把清洗好的正文送进知识库（WeKnora），供语义检索。
它是独立的进程，不挂在派活程序（flows/scheduler.py）的循环上，必须使用独立的 History 数据库连接。
没有配置任何索引目标时，只打印 disabled 就退出。

从哪读起：这个文件只有 main() 一个入口，几行就读完；真正的工作在 db/index_publication.py：run_worker 开若干线程（-c），
每个线程反复调用 process_once 取一个到期的索引作业、上传清洗正文并核对回执；线程 0 每隔 --scan-every 秒用 scan_committed
补扫“已有画像却还没有待办”的版本。新作业平时由清洗段写画像时登记（enqueue_versions）。

读写的表：读 derived_clean_text、derived_profiles、derived_identifiers、history_versions（取正文和检索元数据）；
读写 index_publications（索引作业和状态）、index_targets（索引目标）以及领取轮转、发送限速、闸门统计用的 index_claim_cursor、
index_send_bucket、index_gate_stats。索引表由 History 角色安装：python -m migration.runner --db history --apply。

用法：python -m migration.index_publication worker -c 4     # 跑 worker（4 个线程），Ctrl-C 停
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
