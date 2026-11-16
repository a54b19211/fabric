"""派活程序：常驻循环，每隔几秒一轮，让到期的来源去抓取。它是普通循环，不新建 RQ 任务类型，也没有第二个队列。

从哪读起：main() 里的 while 循环。一轮调一次 fetch/source_refresh.py 的 refresh_once（按 DATAFABRIC_WATCHES 里
各来源的刷新计划建采集任务、放进队列，由 fetch/fetch_job.py 的采集工人去抓），打印这一轮的结果。
来源的两个开关（fabric_sources 的 read_enabled、fetch_enabled，管理员经 /api/fabric/admin/sources 改）关着的 watch 不抓。
--once 只跑一轮，有来源失败或被阻塞时以非零状态退出。

读写的表（都在资料库）：fabric_source_watches（各来源刷新计划的进度）、fabric_runs（新建采集任务、读任务状态）。
图谱（抽取、审核、图发布）已移到另一个项目 Atlas，不在这个仓库。
"""

import argparse
import json
import signal
import time

from sqlalchemy.exc import SQLAlchemyError

from fetch.source_refresh import refresh_once
from store.record_models import FabricError


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds 要在 1~60 秒")
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            refresh = refresh_once()
            failed = any(item.get("state") in {"failed", "blocked"} for item in refresh)
            print(json.dumps({"source_refresh": refresh}, ensure_ascii=False), flush=True)
        except (FabricError, SQLAlchemyError, OSError) as exc:  # 库或配置暂时不可用：下一轮再来，进度都在库里
            failed = True
            code = exc.code if isinstance(exc, FabricError) else type(exc).__name__
            print(json.dumps({"state": "unavailable", "error": code}, ensure_ascii=False), flush=True)
        if args.once:
            if failed:
                raise SystemExit(1)
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
