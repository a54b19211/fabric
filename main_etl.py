"""
==========================================================
  ZK_Data Fabric — 一份资料从抓到存，走哪几步
==========================================================

  这个文件给人读、给单个来源试跑：从上往下，一步一个函数，main() 按顺序调用。
  每个函数只调用常驻程序里已有的函数，不另写第二套逻辑。

    抓  fetch()    按来源目录读一个来源，原件原样存成版本     → history_runs（这次抓取）、history_versions（原件，只追加）
    洗  clean()    出干净正文、找编号、做画像                 → derived_clean_text、derived_identifiers、derived_profiles
        measure()  从清洗正文里读数字（目前只有 who_don 可测）  → derived_measure_runs / _windows，测量对象 derived_measurements
    存  index()    清洗正文送进知识库 WeKnora（配置了才做）     → index_publications 的待办，正文进 WeKnora

  先认识这几个词：
    版本      一份资料在某一时刻的原样存档；内容没变就不多存一个。
    清洗正文  去掉导航、样式后的干净文字，检索和测量读的都是它。
    编号      NCT 试验号、国药准字、PMID、DOI 这类登记号、文号。
    画像      资料是什么类型、来自哪类来源、正文全不全。
    待办      表里等着常驻程序处理的一行；各步靠它衔接，不必互相等。

  线上不是这样跑的：每一步是 run/ 里的一个常驻程序（ops/systemd/ 里的服务），按表里的待办分段做。

  用法（仓库根目录，本仓的 .venv；会写库，请指向临时库）：
    python -m store.migrations.runner --db history --apply   # 先建表
    python main_etl.py --source nmpa --max 5                 # 抓 nmpa 5 条，后面各步都跑一遍
    python main_etl.py --source clinicaltrials               # 来源目录里能抓的来源都行（本地导入 local_files 除外）
    python main_etl.py --only clean --run <运行号>            # 只清洗之前某次抓取留下的版本
  环境变量：DATAFABRIC_HISTORY_DATABASE_URL 必设（历史库）；DATAFABRIC_HISTORY_SOURCE_ALLOWLIST 不设时只放行这次的来源；
  index 要 DATAFABRIC_INDEX_TARGETS、WEKNORA_KDD_URL、WEKNORA_KDD_TOKEN；measure 的模型路线要 DATAFABRIC_MEASURE_LLM_*。
  附件随抓取一起下载（预算用完的记成 not_attempted）；附件的正文由常驻的 run/attachments.py 解析，这里不跑。
"""

import argparse
import os
import time
from collections import Counter

from clean import clean_stage
from fetch import catalog
from fetch.fetch_job import history_fetch_job
from run.measure import run_round as measure_round
from store import history, index_publication
from store.history_models import FetchRequest, HistoryError

STEPS = ("fetch", "clean", "measure", "index")
STATES = {"completed": "完成", "partial": "部分完成（读满 --max 条或到截止时间就停，来源还没读完，这是正常的）", "failed": "失败"}
STOPS = {"upstream_exhausted": "来源读完了", "record_budget": "读满了 --max 条", "page_budget": "翻满了页数上限",
         "deadline": "到了 --deadline 截止时间",
         "reached_known": "读到已存且没变的了（增量读取，前面的都读过）",
         "source_failed": "来源读取失败", "items_unread": "读到底了，但有几条读不到（网址和原因在运行记录的 unread_items 里）"}


def fetch(source, max_records, deadline):
    """抓：按来源目录读一个来源，原件原样存成版本，内容没变的不重复存。打印一行结果，返回这次抓取的运行号。"""
    if source not in catalog.FETCHABLE - {"local_files"}:
        raise SystemExit(f"来源 {source!r} 不能抓。能抓的有：{', '.join(sorted(catalog.FETCHABLE - {'local_files'}))}")
    query, record_ids = catalog.sample_query(source)  # 读法：接口站点 records，网页站点第一个列表页，检索类来源一个检索词
    request = FetchRequest(source_id=source, query=query, source_record_ids=record_ids, max_records=max_records,
                           deadline_seconds=deadline)
    # 任务编号（key）带时间戳：同一个编号会被当成同一次请求，重复提交不会再抓；加上时间戳，每次运行才是新的一次
    run, _ = history.create_fetch(request, key=f"main-etl-{source}-{int(time.time())}")  # 先记一条抓取任务
    history_fetch_job(run.id, run.attempt)  # 这里同步抓完；线上是放进队列，由采集工人去执行
    return report_fetch(run.id)


def report_fetch(run_id):
    """读回这次抓取的结果，打印一行人话总结；一个版本都没有就报错退出（后面各步没有东西可处理）。"""
    run = history.read_run(run_id)
    cover = run.coverage
    why = STOPS.get(cover.get("stop_reason"), cover.get("stop_reason"))
    new, same = cover.get("new_version_count", 0), cover.get("same_version_count", 0)
    print(f"fetch   运行号 {run_id}：状态 {STATES.get(run.state, run.state)}，停止原因 {why}，"
          f"新增 {new} 个版本，{same} 个没变，共 {len(run.version_ids)} 个版本")
    if run.version_ids and isinstance(run.error, dict):  # 部分完成：版本保留，原因在这里（如“有 2 条读不到”）
        print("        ", run.error.get("message"))
    if not run.version_ids:
        reason = (run.error or {}).get("message") or why
        hint = "；网页站点一页常要 30 秒，请加大 --deadline（最大 120）" if cover.get("stop_reason") == "deadline" else ""
        raise SystemExit(f"!!! 没有抓到任何资料（{reason}）{hint}。后面各步不再跑")
    return run_id


def clean(run_id):
    """洗：对这次抓取留下的版本，一次写好干净正文、编号、画像，并登记建索引的待办。规则在 clean/ 目录。"""
    if not run_id:
        raise SystemExit("清洗要知道处理哪次抓取留下的版本：请加 --run <运行号>")
    c = clean_stage.process_versions(history.read_run(run_id).version_ids)
    return (f"新清洗 {c['cleaned']} 份正文、沿用旧结果 {c['reused']} 份、等附件正文 {c['waiting_for_attachment_text']} 份；"
            f"这些版本都有了画像（共 {c['profiled']} 份，已有的直接沿用），登记了 {c.get('index_enqueued', 0)} 个建索引待办")


def counted(items):
    return "、".join(f"{name} {n}" for name, n in Counter(items).items())


def measure():
    """洗：从清洗正文里抽读数，读数直接写成测量对象（标“未核”）。模型路线和规则路线（默认开）都关着时什么也不做。"""
    counts = measure_round()
    if counts.get("state") == "disabled":
        return "没有开启测量（没配模型路线，规则路线也关了），跳过"
    windows = {state: n for state, n in counts["windows"].items() if state != "idle"}
    if not counts["planned"] and not windows:
        return "没有可测量的来源（目前只有 who_don 的正文会被测）或都已测过，跳过"
    return f"新排了 {counts['planned']} 个抽取运行，读了 {sum(windows.values())} 个窗口（{counted(windows)}）"


def index(rounds=20):
    """存：把清洗正文送进知识库（WeKnora）。没配置知识库时什么也不做；先补扫待办，再最多处理 rounds 批到期的待办。"""
    if not index_publication.configured():
        return "没有配置知识库（DATAFABRIC_INDEX_TARGETS），跳过"
    done = Counter()
    try:
        index_publication.scan_committed()  # 第一次跑时登记索引目标，并补上清洗时还没能登记的待办
        for _ in range(rounds):
            batch = index_publication.work_once()
            if not batch:
                break
            done.update(batch)
    except HistoryError as exc:  # 例如没配置桥接服务（WEKNORA_KDD_URL、WEKNORA_KDD_TOKEN）
        return f"失败：{exc.message}（{exc.code}）"
    return f"处理了 {sum(done.values())} 个待办（{counted(done.elements())}）" if done else "没有到期的待办，跳过"


def main(source, max_records, deadline, only=None, run_id=None):
    def on(step):
        return only is None or only == step

    if source:
        os.environ.setdefault("DATAFABRIC_HISTORY_SOURCE_ALLOWLIST", source)
    try:
        if on("fetch"):
            run_id = fetch(source, max_records, deadline)
        if on("clean"):
            print("clean  ", clean(run_id))
    except HistoryError as exc:
        allow = "；请检查 DATAFABRIC_HISTORY_SOURCE_ALLOWLIST 有没有列出这个来源" if exc.code == "source_not_allowed" else ""
        raise SystemExit(f"失败：{exc.message}（{exc.code}）{allow}") from None
    if on("measure"):
        print("measure", measure())
    if on("index"):
        print("index  ", index())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", help="来源编号，如 nmpa、hkex、clinicaltrials（来源目录 fetch/catalog.py）；跑 fetch 时必填")
    parser.add_argument("--max", type=int, default=5, dest="max_records", help="最多抓几条（1~100），默认 5")
    parser.add_argument("--deadline", type=int, default=120, help="抓取的截止秒数（5~120），默认 120；网页站点一页常要 30 秒")
    parser.add_argument("--only", choices=STEPS, help="只跑这一步")
    parser.add_argument("--run", help="清洗用：之前某次抓取的运行号（单独跑 clean 时必填）")
    args = parser.parse_args()
    if not (1 <= args.max_records <= 100 and 5 <= args.deadline <= 120):
        parser.error("--max 要在 1~100，--deadline 要在 5~120")
    if args.only in (None, "fetch") and not args.source:
        parser.error("跑 fetch 要指定来源：请加 --source <来源编号>，如 --source nmpa")
    main(args.source, args.max_records, args.deadline, args.only, args.run)
