"""清洗段的命令行入口：给新抓到的版本写出干净正文、编号和画像，再给画像过的版本打徐子雯的五维质量分。线上用 --loop 常驻
（ops/systemd/datafabric-clean.service）。每个版本只处理一次，结果只追加、可以重跑。

从哪读起：main()，它按参数做两件事之一：清洗加打分（调用 clean/clean_stage.py 的 process，再调 clean/quality_stage.py 的
process，真正的工作都在那两个文件里），或看清洗进度（stage_status）。

读写的表：本文件自己不碰表。清洗经 clean/clean_stage.py：读 fabric_versions 和 fabric_sources；derived_stage_waiting 记
等附件正文的版本；写 derived_clean_text、derived_identifiers、derived_profiles、临床登记差分，并在同一事务里给
index_publications 排活。打分经 clean/quality_stage.py：写 derived_quality_scores、derived_authority_resolutions（药名对齐要
联网，每轮限时 --quality-seconds，0 就不打分；权威库连不上时有药名的版本留到下一轮）。

用法：python -m run.clean --once
      python -m run.clean --loop --poll-seconds 60
      python -m run.clean --status
"""

import argparse
import json
import time

from clean import quality_stage
from clean.clean_stage import process, stage_status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=200)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--quality-seconds", type=int, default=60, help="每轮清洗完给质量分留几秒（0：不打分）")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(stage_status(), ensure_ascii=False, default=str))
        return
    while True:
        print(json.dumps(process(batch=args.batch), ensure_ascii=False), flush=True)
        if args.quality_seconds > 0:
            try:  # 打分出错（例如库还没跑 V043、V044）只打印一行，清洗照常继续
                counts = quality_stage.process(time_budget_seconds=args.quality_seconds)
            except Exception as exc:  # noqa: BLE001
                counts = {"error": (type(exc).__name__ + ": " + str(exc))[:300]}
            print(json.dumps({"quality": counts}, ensure_ascii=False, default=str), flush=True)
        if not args.loop:
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
