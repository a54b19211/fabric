"""v3 3.3 清洗段的运行入口：清洗正文、编号、画像。确定性、可重跑、只追加。

用法：python -m flows.clean_stage --once
      python -m flows.clean_stage --loop --poll-seconds 60
      python -m flows.clean_stage --status
      python -m flows.clean_stage --retry-failed [--source nmpa]   # 死信块受控重置，再跑 --once
"""

import argparse
import json
import time

from db.clean_stage import process, retry_failed, stage_status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=200)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--source")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(stage_status(), ensure_ascii=False, default=str))
        return
    if args.retry_failed:
        print(json.dumps({"reset_chunks": retry_failed(args.source)}))
        return
    while True:
        print(json.dumps(process(batch=args.batch), ensure_ascii=False), flush=True)
        if not args.loop:
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
