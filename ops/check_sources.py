"""体检各来源：对每个来源真抓一次（或两次），打印一张表。

干什么：把 fetch.catalog 里能抓的来源（本地导入 local_files 除外）逐个走一遍和 main_etl.py fetch 相同的路：
  create_fetch 建任务、history_fetch_job 抓、读回结果。看每个来源现在抓不抓得到、用多久、标题和正文是不是空的。
  加 --twice 每个来源抓两次，第二次应该一个都不新增（"新增"列为 0），用来查"没变的内容重复存版本"。
  加 --rounds N 每个来源抓 N 次（--twice 就是 --rounds 2）。
  加 --incremental 按固定刷新的读法抓（连续读到 3 条已存且没变的就停）：第一次和平常一样，第二次起"停止原因"应该是
  reached_known、版本数只有 3 个左右。只对目录里增量键是 stop_at_known 的来源生效；别的来源（有更新窗口的）照常整抓。
  港交所、NCCN 是分组的（一家公司、一份指南一组）：每组各自读到已存的就停。体检只读 --max 条、每次从第一家公司（第一份指南）读起，
  读不完全部；一个完整周期要用固定刷新（DATAFABRIC_HISTORY_WATCHES），历史存量用 backfill_source.py。
怎么跑（仓库根目录，用本仓的 .venv；来源一个接一个跑，不并发，不给对方网站压力）：
  python ops/check_sources.py                          # 全部来源，各抓一次
  python ops/check_sources.py nmpa chictr --twice      # 只测这两个，各抓两次
  python ops/check_sources.py roche --twice --incremental   # 第二次应停在 reached_known
  python ops/check_sources.py --max 3 --deadline 120   # 每个来源最多读几条、最多用几秒（默认就是 3 和 120）
要设的环境变量：
  DATAFABRIC_HISTORY_DATABASE_URL  必设。这个命令会写库（每次抓取留下运行记录和版本），一定要指向临时库，别连线上库；
                                   库先建好表：python -m store.migrations.runner --db history --apply
  GDELT 常回 429（对方限流），读不到也如实显示。
有来源没抓到（失败，或一个版本都没有）时，表打完以退出码 1 结束。表打在标准输出，日志在标准错误输出（只想看表加 2>/dev/null）。
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from fetch import catalog  # noqa: E402
from fetch.fetch_job import history_fetch_job  # noqa: E402
from fetch.windows import KNOWN_STOP  # noqa: E402
from store import history  # noqa: E402
from store.history_models import FetchRequest  # noqa: E402

COLUMNS = ("来源", "第几次", "状态", "停止原因", "版本", "新增", "没变", "秒", "请求", "空标题", "空日期", "空正文", "错误")


def blank_counts(version_ids):
    """(标题为空的版本数, 日期为空的版本数, 正文为空的版本数)，读的是 history_versions 里存下的投影。附件版本不算：
    它的正文是加工层以后解析的。日期：发布日期，没有就看事件日期（如 FDA 信函的签字日期）；两个都没有才算空。"""
    if not version_ids:
        return 0, 0, 0
    with history.history_engine().connect() as conn:
        rows = conn.execute(text("select payload->>'title', coalesce(payload->'source_published_at'->>'value', "
                                 "payload->'event_time'->>'value'), payload->>'clean_text' from history_versions "
                                 "where version_id = any(:ids) and coalesce(payload->'facts'->>'role', '') <> 'attachment'"),
                            {"ids": list(version_ids)}).all()
    return (sum(not (title or "").strip() for title, _, _ in rows), sum(not date for _, date, _ in rows),
            sum(not (body or "").strip() for _, _, body in rows))


def fetch_once(source, attempt, max_records, deadline, known_stop=0):
    """抓一次，返回表里的一行（和 COLUMNS 一一对应）。抓取抛出的异常不隐藏，写进"错误"列。"""
    query, record_ids = catalog.sample_query(source)
    request = FetchRequest(source_id=source, query=query, source_record_ids=record_ids, max_records=max_records,
                           deadline_seconds=deadline, known_stop=known_stop)
    run, _ = history.create_fetch(request, key=f"check-{source}-{attempt}-{time.time_ns()}")
    crashed, start = "", time.monotonic()
    try:
        history_fetch_job(run.id, run.attempt)
    except Exception as exc:  # noqa: BLE001 -- 体检要看到每一种失败
        crashed = f"{type(exc).__name__}: {exc}"
    seconds = round(time.monotonic() - start, 1)
    result = history.read_run(run.id)
    cover = result.coverage or {}
    error = result.error.get("message") if isinstance(result.error, dict) else result.error
    no_title, no_date, no_text = blank_counts(result.version_ids)
    return (source, attempt, result.state, cover.get("stop_reason"), len(result.version_ids),
            cover.get("new_version_count"), cover.get("same_version_count"), seconds, cover.get("requests"),
            no_title, no_date, no_text, (crashed or error or "")[:80])


def main():
    parser = argparse.ArgumentParser(description="体检各来源：各抓一次或两次，打印一张表（会写库，要指向临时库）")
    parser.add_argument("sources", nargs="*", help="只测这几个来源，默认全部")
    parser.add_argument("--twice", action="store_true", help="每个来源抓两次，第二次应该一个都不新增")
    parser.add_argument("--rounds", type=int, default=0, help="每个来源抓几次（默认 1，--twice 是 2）")
    parser.add_argument("--incremental", action="store_true", help="按固定刷新的读法抓：连续读到 3 条已存且没变的就停")
    parser.add_argument("--max", type=int, default=3, dest="max_records", help="每次最多读几条（默认 3）")
    parser.add_argument("--deadline", type=int, default=120, help="每次最多用几秒（默认 120）")
    args = parser.parse_args()
    known = sorted(catalog.FETCHABLE - {"local_files"})  # local_files 是本地导入，不对外抓
    chosen = args.sources or known
    if [s for s in chosen if s not in known]:
        parser.error("不认识的来源：" + ", ".join(s for s in chosen if s not in known) + "；能测的有：" + ", ".join(known))
    if not os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL"):
        parser.error("要先设 DATAFABRIC_HISTORY_DATABASE_URL，并且它必须是临时库：这个命令会写库（用法见文件开头）")
    print(" | ".join(COLUMNS), flush=True)
    bad = 0
    for source in chosen:
        for attempt in range(1, (args.rounds or (2 if args.twice else 1)) + 1):
            known_stop = KNOWN_STOP if args.incremental and catalog.spec(source).incremental.kind == "stop_at_known" else 0
            row = fetch_once(source, attempt, args.max_records, args.deadline, known_stop)
            print(" | ".join("" if value is None else str(value) for value in row), flush=True)
            bad += row[2] == "failed" or row[4] == 0
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
