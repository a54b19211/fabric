"""派活程序：常驻循环，每隔几秒一轮，让到期的来源去抓取，把待办发表出去，并写一次心跳。
它是普通循环，不新建 RQ 任务类型，也没有第二个队列。

从哪读起：main() 里的 while 循环。一轮依次调用 fetch/source_refresh.py 的 refresh_once（按各来源的刷新计划建采集任务、放进队列，
由 fetch/fetch_job.py 的采集工人去抓）、graph/publish/publication.py 的 publish_once（发表原采集库里的待办，最多 --batch-size 项；
现在实际发表的是审核图，旧文档库存的事件因旧文档冻结桥已退役而标为阻塞）和 publisher_heartbeat（写心跳），然后打印这一轮的结果。
--once 只跑一轮，有来源失败、发表失败或被阻塞时以非零状态退出；--reconcile <高水位> 只读对账后退出。

读写的表：publication_outbox（原采集库里的待办表：发表待办是 kind 为 document 或 graph 的行；各来源的刷新计划和本程序的心跳是
kind='control' 的行）；history_runs（刷新时新建采集任务、读任务状态）；history_versions 和 history_sources（发表时写入固定版本）。
对账只读 documents 和 history_versions。
"""

import argparse
import json
import signal
import time

from sqlalchemy.exc import SQLAlchemyError

from fetch.source_refresh import refresh_once
from graph.publish.publication import publish_once, publisher_heartbeat
from store.history_models import HistoryError


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reconcile", type=int, metavar="HIGH_WATERMARK",
                        help="只读对账：documents id≤高水位的每一行都已按当前内容发表，打印结果后退出")
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    if args.reconcile is not None:
        from graph.publish.publication import reconcile

        print(json.dumps(reconcile(args.reconcile), ensure_ascii=False))
        return
    if not 1 <= args.poll_seconds <= 60 or not 1 <= args.batch_size <= 100:
        parser.error("poll 1..60秒、batch 1..100")
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        failed = False
        try:
            publisher_heartbeat("running")
            refresh = refresh_once()
            failed = any(item.get("state") in {"failed", "blocked"} for item in refresh)
            states = []
            for _ in range(args.batch_size):
                if stopping:
                    break
                result = publish_once()
                states.append(result["state"])
                failed = failed or result["state"] in {"failed", "blocked"}
                if result["state"] in {"idle", "resource_blocked"}:
                    break
            publisher_heartbeat(
                "resource_blocked"
                if "resource_blocked" in states
                else "idle"
                if states == ["idle"]
                else "running"
            )
            print(
                json.dumps(
                    {
                        "processed": states,
                        "source_refresh": refresh,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (HistoryError, SQLAlchemyError, OSError):
            failed = True
            try:
                publisher_heartbeat("unavailable")
            except (HistoryError, SQLAlchemyError, OSError):
                print(
                    '{"state":"heartbeat_unavailable"}', flush=True
                )  # 旧心跳自然过期。
            print(
                json.dumps(
                    {
                        "state": "unavailable",
                        "message": "发表数据库或配置暂不可用；游标/待办保留",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        if args.once:
            if failed:
                raise SystemExit(1)
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
