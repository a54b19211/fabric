"""
==========================================================
  ZK_Data Fabric — 一份资料从抓取到能被检索，走哪几步
==========================================================

  这个文件是给人读、给单次调试用的：从上往下，一步一个函数，main() 按顺序调用。
  每个函数只调用后台程序里已有的函数，不另写第二套逻辑。

先认识这几个词：
  运行        一次抓取任务（抓哪个站点、抓几条、结果如何），在 history_runs 里占一行；运行号就是这一行的编号。
  版本        一份资料在某一时刻的原样存档，存在 history_versions，只追加、不修改；内容没变就不会多出新版本（网页只看解析出的正文和附件清单：页面 HTML 里解析没读到的变化不算新版本，存下的是第一次看到的 HTML）。
  清洗正文    从原件里去掉导航、样式后剩下的干净文字，后面的检索和测量读的都是它。
  编号        资料里出现的登记号、文号、DOI 等（NCT 试验号、国药准字、PMID、DOI……），清洗时一并找出来存好。
  画像        给一份资料贴的标签：是什么类型的资料、来自哪一类来源、格式好不好（正文缺不缺、能不能读）。
  待办        表里等着后台程序去处理的一行，如"这份正文该送进知识库"；程序做完就改它的状态，所以各步不必互相等。
  知识库      WeKnora，做语义检索的服务；清洗正文送进去之后，才能按意思（不只是关键词）搜到。
  读数、测量对象  读数是正文里读出的一个数字和它的位置（"确诊 12 例"里的 12）；整理成"谁的什么指标是多少"，
              才是测量对象（derived_measurements）。
  核验        BioCheck 服务逐条对照原文，核对 Fabric 抽出的编号、画像、读数对不对，结果写进 derived_extraction_checks。
  对齐、评分  徐子雯的两项工作：对齐是把药名查到 PubChem/ChEMBL 的标准编号，评分是给资料打五维质量分并定等级；
              都还没接入，这里只占位。

  一份资料走的路，和每一步存在哪张表里：

    1. fetch    按站点配置抓网页或接口      → history_runs（这次抓取的记录）、history_versions（原件原样存下，只追加）
    2. clean    出干净正文、找编号、做画像   → derived_clean_text、derived_identifiers、derived_profiles；
                                              配置了知识库时还会登记建索引的待办 index_publications；另登记核验任务 derived_extraction_check_tasks
    3. align    药名对到标准编号（徐子雯）   → （接入后才有）
    4. score    五维质量分和等级（徐子雯）   → （接入后才有）
    5. index    清洗正文送进知识库           → 处理 index_publications 的待办，正文进 WeKnora
    6. measure  从正文里抽数字读数           → derived_measure_runs / _windows / _readings（读数）
                                              读数变成对象、交核验服务核对，由 clean/extraction_checks.py 另一个后台程序做，这里不跑
                                              目前只有 who_don 的正文会被测，而这里抓不了 who_don，所以 measure 一般会"跳过"

  线上不是这样跑的：每一步由常驻的后台程序分段跑，靠表里的待办衔接，互不等待——
    派活：run/scheduler.py（到点给各来源建采集任务，放进队列）
    fetch：fetch/fetch_job.py（采集工人）    clean：run/clean.py
    index：run/index.py worker    measure：run/measure.py
    核验读数：clean/extraction_checks.py
  这里把同样的函数按顺序串起来，为的是读懂和对单个站点试跑；线上仍以后台程序为准。

  先要有一个建好表的历史库：python -m store.migrations.runner --db history --apply（指向临时库）。
  运行前要设的环境变量（线上放在 ~/.config/datafabric-publication/runtime.env）。这里会写库，请指向临时库，别连线上库：
    DATAFABRIC_HISTORY_DATABASE_URL      必设。历史资料数据库
    DATAFABRIC_HISTORY_SOURCE_ALLOWLIST  必设。允许处理的来源，逗号分隔，如 nmpa,fda_api_drug_label；
                                         没列进去的站点会报"当前许可不允许该来源"（抓取、清洗、测量都按这份名单）
    DATAFABRIC_HISTORY_FETCH_ENABLED     抓取总开关，默认 true（开）；设成别的值就关掉公开来源抓取
    index 可选：DATAFABRIC_INDEX_TARGETS（送进哪些知识库；没有目标就跳过这一步）、WEKNORA_KDD_URL 和 WEKNORA_KDD_TOKEN
                （WeKnora 桥接服务的地址和口令）、DATAFABRIC_INDEX_*（各种上限）
    measure 可选：DATAFABRIC_MEASURE_LLM_BASE_URL 和 DATAFABRIC_MEASURE_LLM_MODEL（模型路线，设了才有）、
                DATAFABRIC_MEASURE_RULES_ROUTE（规则路线，默认开，设 false 关掉）、DATAFABRIC_MEASURE_ROUND_REQUESTS（一轮最多发几次模型请求）

  运行方式（仓库根目录，用本仓的 .venv）：
    python main_etl.py --site nmpa --max 5          # 抓 nmpa 5 条，然后后面各步都跑一遍
    python main_etl.py --only fetch --site nmpa     # 只跑某一步：fetch clean align score index measure
    python main_etl.py --only clean --run <运行号>  # 清洗之前某次抓取留下的版本
  运行号 fetch 抓完会打印；忘了就查历史库（只读），第一行是最近一次：
    SELECT id, source_id, state, created_at FROM history_runs WHERE kind = 'fetch' ORDER BY created_at DESC LIMIT 5;
  --site 只认 fetch/sites 里配置的网页、接口站点键（填错时报错会列出全部）。来源目录里的 clinicaltrials、pubmed、
  who_don 等走别的插件，这里抓不了。网页站点一页常要 30 秒，所以 --deadline（抓取截止秒数）默认给到上限 120。
  这里不下载附件（附件会记成"未尝试"），线上由采集工人处理。
  抓到 0 个版本时 fetch 会醒目地说明原因并以非 0 退出，后面各步不再跑。
"""

import argparse
import time
from collections import Counter

from clean import clean_stage
from fetch.fetch_job import history_fetch_job
from fetch.site_profiles import SITE_PROFILES
from run.measure import run_round as measure_round
from store import history, index_publication
from store.history_models import FetchRequest, HistoryError

STEPS = ("fetch", "clean", "align", "score", "index", "measure")
STATES = {"completed": "完成", "partial": "部分完成（读满 --max 条或到截止时间就停，来源还没读完，这是正常的）", "failed": "失败"}
STOPS = {"upstream_exhausted": "来源读完了", "record_budget": "读满了 --max 条", "page_budget": "翻满了页数上限",
         "deadline": "到了 --deadline 截止时间", "byte_budget": "下载量到了上限",
         "reached_known": "读到已存且没变的了（增量读取，前面的都读过）",
         "outbound_quota_day": "本机今天对该站的请求数到了上限", "outbound_quota_hour": "本机这一小时对该站的请求数到了上限",
         "source_failed": "来源读取失败", "items_unread": "读到底了，但有几条读不到（网址和原因在运行记录的 unread_items 里）"}


def fetch(site, max_records, deadline):
    """按站点配置抓取，原件原样存成固定版本，内容没变的不重复存。打印一行结果，返回这次抓取的运行号。"""
    if not site:
        raise SystemExit("跑 fetch 要指定站点：请加 --site <站点键>，如 --site nmpa")
    if site not in SITE_PROFILES:
        raise SystemExit(f"站点 {site!r} 不能用。--site 只认 fetch/sites 里配置的网页、接口站点键：{', '.join(sorted(SITE_PROFILES))}；"
                         "来源目录里的 clinicaltrials、who_don 等走别的插件，这里抓不了")
    profile = SITE_PROFILES[site]
    # 查询词（FetchRequest.query）决定读法：接口站点固定填 records，网页站点填配置里的第一个列表页网址
    query = "records" if profile.get("source_type") == "api" else profile["start_urls"][0]
    request = FetchRequest(source_id=site, query=query, max_records=max_records, deadline_seconds=deadline)
    # 任务编号（key）带时间戳：同一个编号会被当成同一次请求，重复提交不会再抓；加上时间戳，每次运行才是新的一次
    run, _ = history.create_fetch(request, key=f"pipeline-{site}-{int(time.time())}")  # 先记一条抓取任务（run）
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
    """对这次抓取留下的版本，一次写好：干净正文、编号、画像，并登记建索引的待办和核验任务。清洗规则在 clean/ 目录。"""
    if not run_id:
        raise SystemExit("清洗要知道处理哪次抓取留下的版本：请加 --run <运行号>")
    c = clean_stage.process_versions(history.read_run(run_id).version_ids)
    return (f"新清洗 {c['cleaned']} 份正文、沿用旧结果 {c['reused']} 份、等附件正文 {c['waiting_for_attachment_text']} 份；"
            f"这些版本都有了画像（共 {c['profiled']} 份，已有的直接沿用），登记了 {c.get('index_enqueued', 0)} 个建索引待办、{c.get('checks_enqueued', 0)} 个核验任务")


def align():
    """（接入徐子雯的对齐后才有）药名 → PubChem/ChEMBL 标准编号。"""
    return "未接入"


def score():
    """（接入徐子雯的评分后才有）五维质量分和等级。"""
    return "未接入"


def counted(items):
    return "、".join(f"{name} {n}" for name, n in Counter(items).items())


def index(rounds=20):
    """把清洗正文送进知识库（WeKnora）。没配置知识库时什么也不做；最多处理 rounds 个到期的待办。"""
    if not index_publication.configured():
        return "没有配置知识库（DATAFABRIC_INDEX_TARGETS），跳过"
    done = []
    for _ in range(rounds):
        state = index_publication.process_once()["state"]
        if state == "idle":
            break
        done.append(state)
    return f"处理了 {len(done)} 个待办（{counted(done)}）" if done else "没有到期的待办，跳过"


def measure():
    """从清洗正文里抽读数，读数落进表里。模型路线和规则路线（默认开）都关着时什么也不做。"""
    counts = measure_round()
    if counts.get("state") == "disabled":
        return "没有开启测量（没配模型路线，规则路线也关了），跳过"
    windows = {state: n for state, n in counts["windows"].items() if state != "idle"}
    if not counts["planned"] and not windows:
        return "没有可测量的来源（目前只有 who_don 的正文会被测）或都已测过，跳过"
    return f"新排了 {counts['planned']} 个抽取运行，读了 {sum(windows.values())} 个窗口（{counted(windows)}）"


def main(site, max_records, deadline, only=None, run_id=None):
    def on(step):
        return only is None or only == step

    try:
        if on("fetch"):
            run_id = fetch(site, max_records, deadline)
        if on("clean"):
            print("clean  ", clean(run_id))
    except HistoryError as exc:
        allow = "；请检查 DATAFABRIC_HISTORY_SOURCE_ALLOWLIST 有没有列出这个来源" if exc.code == "source_not_allowed" else ""
        raise SystemExit(f"失败：{exc.message}（{exc.code}）{allow}") from None
    if on("align"):
        print("align  ", align())
    if on("score"):
        print("score  ", score())
    if on("index"):
        print("index  ", index())
    if on("measure"):
        print("measure", measure())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", help="站点键，只认 fetch/sites 里配置的网页、接口站点（如 nmpa），"
                                       "不含 clinicaltrials、who_don 等；跑 fetch 时必填")
    parser.add_argument("--max", type=int, default=5, dest="max_records", help="最多抓几条（1~100），默认 5")
    parser.add_argument("--deadline", type=int, default=120, help="抓取的截止秒数（5~120），默认 120；网页站点一页常要 30 秒")
    parser.add_argument("--only", choices=STEPS, help="只跑这一步")
    parser.add_argument("--run", help="清洗用：之前某次抓取的运行号（单独跑 clean 时必填）")
    args = parser.parse_args()
    if not (1 <= args.max_records <= 100 and 5 <= args.deadline <= 120):
        parser.error("--max 要在 1~100，--deadline 要在 5~120")
    main(args.site, args.max_records, args.deadline, args.only, args.run)
