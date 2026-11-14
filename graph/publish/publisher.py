"""图发布的常驻程序（唐组）：每隔几秒一轮，把原采集库（DATAFABRIC_PUBLICATION_DATABASE_URL）publication_outbox 里的
待办发表进资料库（现在实际发表的是审核图；旧文档库存的事件因旧文档冻结桥已退役而标为阻塞），并写一次心跳。
抓、洗、存不依赖它：没配原采集库时只是这个程序不工作。

从哪读起：main() 里的 while 循环：一轮最多发表 --batch-size 项（graph/publish/publication.py · publish_once），
再写心跳 publisher_heartbeat（/api/fabric/publication/status 读它）。--once 只跑一轮，发表失败或被阻塞时以非零状态退出；
--reconcile <高水位> 只读对账后退出。服务文件 ops/systemd/datafabric-graph-publisher.service。

    python -m graph.publish.publisher --once
"""

import argparse
import json
import signal
import time

from sqlalchemy.exc import SQLAlchemyError

from graph.publish.publication import publish_once, publisher_heartbeat, reconcile
from store.record_models import FabricError


def _round(batch_size, stopping):
    """一轮：最多发表 batch_size 项，写心跳。返回 (各项的状态, 有没有失败)。"""
    publisher_heartbeat("running")
    states = []
    for _ in range(batch_size):
        if stopping():
            break
        states.append(publish_once()["state"])
        if states[-1] in {"idle", "resource_blocked"}:
            break
    publisher_heartbeat("resource_blocked" if "resource_blocked" in states else "idle" if states == ["idle"]
                        else "running")
    return states, any(state in {"failed", "blocked"} for state in states)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reconcile", type=int, metavar="HIGH_WATERMARK",
                        help="只读对账：documents id≤高水位的每一行都已按当前内容发表，打印结果后退出")
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    if args.reconcile is not None:
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
        try:
            states, failed = _round(args.batch_size, lambda: stopping)
            print(json.dumps({"processed": states}, ensure_ascii=False), flush=True)
        except (FabricError, SQLAlchemyError, OSError):
            failed = True
            try:
                publisher_heartbeat("unavailable")
            except (FabricError, SQLAlchemyError, OSError):
                pass  # 心跳也写不进：旧心跳自然过期
            print(json.dumps({"state": "unavailable", "message": "发表数据库或配置暂不可用；待办保留"},
                             ensure_ascii=False), flush=True)
        if args.once:
            if failed:
                raise SystemExit(1)
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
