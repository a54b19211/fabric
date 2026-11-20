"""加工层附件解析的运行入口（V023）。解析在事务外进行，可与发布器并行；重复运行不重复处理。

用法：
  python -m run.attachments --once [--batch 20]
  python -m run.attachments --loop --poll-seconds 60
  python -m run.attachments --once --batch 500 --source nmpa --source cde   # 只做这几个来源的附件
"""

import argparse
import json
import time

from clean.derived_text import process


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--source", action="append", default=[], help="只做这个来源的附件（可写多次）；不给就是全部")
    args = parser.parse_args()
    while True:
        scope = {"sources": args.source} if args.source else {}
        print(json.dumps(process(batch=args.batch, **scope), ensure_ascii=False), flush=True)
        if not args.loop:
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
