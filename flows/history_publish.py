"""持续发表原采集待办；普通循环，不新建RQ任务类型或第二个队列。"""

import argparse
import json
import signal
import time

from sqlalchemy.exc import SQLAlchemyError

from biz_rules.history_models import HistoryError
from db.publication import backfill_once, publish_once, publisher_heartbeat
from db.source_refresh import refresh_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--backfill-batch-size",
        type=int,
        default=1,
        help="每轮存量最多补录数；0只处理已捕获待办",
    )
    args = parser.parse_args()
    if (
        not 1 <= args.poll_seconds <= 60
        or not 1 <= args.batch_size <= 100
        or not 0 <= args.backfill_batch_size <= 100
    ):
        parser.error("poll 1..60秒、batch 1..100、backfill-batch 0..100")
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
            seeded = (
                backfill_once(limit=args.backfill_batch_size)
                if args.backfill_batch_size
                else {
                    "seeded": 0,
                    "complete": False,
                    "state": "disabled",
                    "scope": "initial_inventory_not_completed",
                }
            )
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
                        "backfill": seeded,
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
