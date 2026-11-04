"""加工层附件解析的运行入口（V023）。解析在事务外进行，可与发布器并行；重复运行不重复处理。

用法：
  python -m flows.process_attachments --once [--batch 20]
  python -m flows.process_attachments --loop --poll-seconds 60
"""

import argparse
import json
import time

from db.derived_text import process


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    while True:
        print(json.dumps(process(batch=args.batch), ensure_ascii=False), flush=True)
        if not args.loop:
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
