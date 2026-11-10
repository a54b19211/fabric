"""清洗段的命令行入口：给新抓到的版本写出干净正文、编号和画像。线上用 --loop 常驻
（ops/systemd/datafabric-clean.service）。每个版本只处理一次，结果只追加、可以重跑。

从哪读起：main()，它按参数做三件事之一：清洗（调用 db/clean_stage.py 的 process，真正的工作都在那里）、
看进度（stage_status）、把死信块重置（retry_failed）。接着读 db/clean_stage.py。

读写的表：本文件自己不碰表，都经 db/clean_stage.py：读 history_versions 和 history_sources；用 derived_stage_chunks 和
derived_stage_waiting 做工作队列；写 derived_clean_text、derived_identifiers、derived_profiles，并在同一事务里给
index_publications 和 derived_extraction_check_tasks 排活。

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
