"""把一个来源的历史存量一次读到底：不设条数上限，按页往后读，可中断后接着读。

和固定刷新（DATAFABRIC_HISTORY_WATCHES）是两回事：固定刷新每轮只读最新的、遇到已存的就停；这个命令是手动补老的，
不停在已存的上，一直读到来源读完。读的方式、存版本的规则都和 main_etl.py fetch 一样（create_fetch 建任务、
history_fetch_job 抓），只是一段接一段：每段最多 100 条（一次读取的上限），上一段给的续读位置交给下一段。
怎么跑（仓库根目录，用本仓的 .venv；一个接一个地读，不并发）：
  python ops/backfill_source.py roche                      # 接口站点：查询词是 records，不用给
  python ops/backfill_source.py chictr                     # 网页站点：默认读站点配置里的第一个列表入口
  python ops/backfill_source.py nmpa --query <另一个入口网址>  # 站点有几个列表入口时，一个一个来
  python ops/backfill_source.py pubmed --query "BRCA1"     # PubMed 要给检索词（ESearch 最多翻到前 1 万条，更多的把检索词按年份切开）
  python ops/backfill_source.py chictr --pages 12000       # 列表站点默认只读站点配置里的 max_pages 页（ChiCTR 配的是 50 页，全站约 12000 页）；--pages 盖过它
可中断：Ctrl-C 或进程被停了，再跑同一条命令就接着读。每一段有固定的任务键（来源、读取入口、第几段、第几次），
已经读完的段直接取回结果、不再发请求；停在半路没读完的那一段重读（它没存完的版本本来就没存）。
一段失败（如对方网站出错）命令停下并说原因；再跑一次，这一段换个新的任务键重读。
下面几种情况命令也会停下并说明，不会原地打转：续读位置没前进、又一个版本都没读到（常见：接口站点重放前面的条目就用完了
--deadline，调大它再试）。
--deadline 每段的秒数，5 到 120；它和读取入口、--pages 一起进每一段的任务键（换了值就是另一次回填，不会和旧的任务键冲突）。
要设的环境变量：DATAFABRIC_HISTORY_DATABASE_URL 必设（要补哪个库就指向哪个库，这个命令会写库）；
DATAFABRIC_HISTORY_SOURCE_ALLOWLIST 不用设，命令把它设成这个来源。限速照常生效。
限制：接口站点 Roche、FiercePharma 每一段都要把前面的重新走一遍才接得上（它们的拉取函数没有可恢复的游标），条目多时越往后越慢；
港交所（续读位置是第几家公司、这家的第几条）、NCCN（第几份指南）从续读位置接着读，不重走前面的；列表站点只重翻列表页，不重读详情页。
"""

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetch import catalog  # noqa: E402
from fetch.fetch_job import history_fetch_job  # noqa: E402
from fetch.site_profiles import SITE_PROFILES  # noqa: E402
from store import history  # noqa: E402
from store.history_models import FetchRequest  # noqa: E402

LEG_RECORDS = 100  # 一次读取最多 100 条（FetchRequest.max_records 的上限）


def read_leg(source, query, continuation, leg, deadline, tag):
    """读一段，返回读完的运行。同一段重跑：已读完的直接取回；失败的换新任务键再读；停在半路的标成中断再读。
    tag：读取入口、deadline、pages 的摘要，进任务键（同一个键不能对应不同的请求）。"""
    request = FetchRequest(source_id=source, query=query, max_records=LEG_RECORDS, deadline_seconds=deadline,
                           continuation=continuation)
    for attempt in range(1, 1000):
        run, _ = history.create_fetch(request, key=f"backfill:{source}:{tag}:{leg}:{attempt}")
        if run.state == "running":  # 上一次的进程被硬杀，没来得及标中断（一次只跑一个这个命令）：当作中断，换键重读
            history.fail_job(run.id, run.attempt, "interrupted", "回填被中断，这一段重读")
        elif run.state == "queued":
            try:
                history_fetch_job(run.id, run.attempt)
            except BaseException:
                history.fail_job(run.id, run.attempt, "interrupted", "回填被中断，这一段重读")
                raise
            return history.read_run(run.id)
        elif run.state in ("completed", "partial") and not run.error:
            return run  # 上一次已经读完的段
        # 其余（失败的段，带着错误的“部分完成”）：换个新任务键，这一段重读
    raise SystemExit("这一段已失败太多次，请先看原因（history_runs 里键以 backfill:%s 开头的运行）" % source)


def why_stop(run, cover, message, before, read_nothing):
    """这一段之后要不要停：(说明, 是不是读到底了)；说明是 None 就接着读下一段。before：这一段开始时的续读位置。"""
    continuation = cover.get("next_continuation")
    if cover.get("upstream_pagination_exhausted") or cover.get("stop_reason") == "items_unread":
        # items_unread：读到底了，只是有几条读不到（它们的网址和原因在那一段的 unread_items 里）
        return "读到底了。", True
    if continuation and continuation == before and read_nothing:
        return "停在这里：续读位置没有前进，这一段一个版本也没读到（接口站点要先重放前面的条目，可以把 --deadline 调大）。", False
    if run.state == "failed" or not continuation:
        return "停在这里：" + (message or "来源没给续读位置") + "。", False
    return None, False


def seconds(text):
    """--deadline 的值：5 到 120 的整数（FetchRequest.deadline_seconds 的范围，超出范围到最后才被拒）。"""
    value = int(text)
    if not 5 <= value <= 120:
        raise argparse.ArgumentTypeError("要在 5 到 120 之间")
    return value


def main():
    parser = argparse.ArgumentParser(description="把一个来源的历史存量一次读到底（会写库，可中断后接着读）")
    parser.add_argument("source", help="来源编号，如 roche、chictr、fda_crl")
    parser.add_argument("--query", help="读取入口：站点的列表入口网址、PubMed 检索词……；不给就用 fetch/catalog.py 的 sample_query")
    parser.add_argument("--deadline", type=seconds, default=120, metavar="5-120", help="每一段最多用几秒（5 到 120，默认 120）")
    parser.add_argument("--pages", type=int, help="列表网页站点最多读几页，盖过站点配置里的 max_pages")
    args = parser.parse_args()
    known = sorted(catalog.FETCHABLE - {"local_files"})
    if args.source not in known:
        parser.error("不认识的来源：%s；能读的有：%s" % (args.source, ", ".join(known)))
    if not os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL"):
        parser.error("要先设 DATAFABRIC_HISTORY_DATABASE_URL：这个命令会写库（用法见文件开头）")
    os.environ["DATAFABRIC_HISTORY_SOURCE_ALLOWLIST"] = args.source
    query = args.query or catalog.sample_query(args.source)[0]
    profile = SITE_PROFILES.get(args.source, {})
    list_site = "max_pages" in profile and profile.get("source_type") != "api"  # 按列表页翻页的网页站点：读到的页数受 max_pages 限制
    if args.pages:
        if not list_site:
            parser.error("--pages 只对列表网页站点有用（%s 不是）" % args.source)
        profile["max_pages"] = args.pages  # 这个命令在本进程里读，改的只是这一次
    tag = hashlib.sha256(f"{query}|{args.deadline}|{args.pages}".encode()).hexdigest()[:8]
    continuation, leg, total = None, 1, {"新增": 0, "没变": 0, "读不到": 0}
    while True:
        started = time.monotonic()
        run = read_leg(args.source, query, continuation, leg, args.deadline, tag)
        cover = run.coverage or {}
        message = run.error.get("message") if isinstance(run.error, dict) else run.error
        new, same, unread = cover.get("new_version_count", 0), cover.get("same_version_count", 0), len(cover.get("unread_items", []))
        total["新增"] += new
        total["没变"] += same
        total["读不到"] += unread
        print(f"第 {leg} 段：{run.state}，新增 {new}，没变 {same}，读不到 {unread}，停止原因 {cover.get('stop_reason')}，"
              f"{time.monotonic() - started:.0f} 秒" + (f"；{message}" if message else ""), flush=True)
        stop, finished = why_stop(run, cover, message, continuation, new + same == 0)
        if stop is None:
            continuation = cover.get("next_continuation")
            leg += 1
            continue
        print(stop + "累计：" + "，".join(f"{k} {v}" for k, v in total.items()))
        if finished and list_site:
            print("注意：列表网页站点只读到站点配置的页数（%s 页，--pages 可以改）；站点的列表更长的话，后面的页没有读。"
                  % profile["max_pages"])
        return 0 if finished else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("被中断，这一段已标成中断。再跑同一条命令就接着读。")
