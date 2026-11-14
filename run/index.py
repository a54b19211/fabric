"""索引 worker 的启动入口：把清洗好的正文送进知识库（WeKnora），供语义检索。
它是独立的进程，不挂在派活程序（run/scheduler.py）的循环上，用自己的资料库连接。
没有配置任何索引目标时，只打印 disabled 就退出。

从哪读起：这个文件只有 main()；真正的工作在 store/index_publication.py 的 run_worker：一个循环，每轮先按间隔补扫
（scan_committed：已有画像却还没有待办的版本），再领一批到期待办（work_once）发给桥接服务、记下结果；没活就睡几秒。
新待办平时由清洗段写画像时登记（enqueue_versions）。

读写的表：读 derived_clean_text、derived_profiles、derived_identifiers、fabric_versions（取正文和检索元数据）；
读写 index_publications（索引待办和状态）、index_targets（索引目标）。表由 fabric 角色安装：
python -m store.migrations.runner --db fabric --apply。

用法：python -m run.index      # 跑 worker，Ctrl-C 停
"""

import argparse
import json
import signal
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan-every", type=float, default=30.0, help="回填扫描间隔（秒）")
    args = parser.parse_args()
    from store import index_publication

    if not index_publication.configured():
        print(json.dumps({"index": {"state": "disabled"}}), flush=True)
        return
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    counts = index_publication.run_worker(scan_every_seconds=args.scan_every, stop=stop)
    print(json.dumps({"index": {"state": "stopped", "processed": counts}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
